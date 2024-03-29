import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.model_zoo as model_zoo
from collections import OrderedDict
import numpy as np
from torch.autograd import Variable, Function
import time

class BoxCarFunc(Function):
    
    def h(self, x):
        k=0.1
        return 1.0/(1.0 + torch.exp(-k * x))
    def diff_h(self, x):
        k=0.1
        return k * torch.exp(-k * x) * self.h(x) ** 2

    def forward(self, x, m, f1, f2, use_gpu):
        '''
            x -  s x ch x dim1 x dim2
            m - s x g x 4
            returns - s x g x ch x dim1 x dim2
        '''

        self.save_for_backward(x, m, use_gpu, f1, f2)
        
        s = x.size(0)
        ch = x.size(1)
        dim1 = x.size(2)
        dim2 = x.size(3)

        g = m.size(1)
        M = []
        for i in range(g):
            m1 = self.h((f1 + -1*m[:, i, 0].contiguous().view(s, 1, 1, 1, -1)).float())
            m2 = self.h((f1 + -1*m[:, i, 2].contiguous().view(s, 1, 1, 1, -1)).float())
            m3 = self.h((f2 + -1*m[:, i, 1].contiguous().view(s, 1, 1, 1, -1)).float())
            m4 = self.h((f2 + -1*m[:, i, 3].contiguous().view(s, 1, 1, 1, -1)).float())
            v = (m1 + -1*m2)*(m3 + -1*m4)
            M.append(v)

        M = torch.cat(M, 1)
        return x.view(s, 1, ch, dim1, dim2) * M

    def backward(self, dL_dg):
        # dL_dg: (s, g, 3, 299, 299)
        # self.x: (s, 3, 299, 299)
        # self.m: (s, g, 4)
        #dL_dg = torch.abs(dL_dg)
        #max_val = torch.max(dL_dg)
        #if max_val:
        #    dL_dg = dL_dg / torch.max(dL_dg) * 0.0000001
        #print('Max Before:', torch.max(dL_dg), "Min Before: ", torch.min(dL_dg))
        x, M, use_gpu, I, J = self.saved_tensors
        g = M.size(1)
        s = x.size(0)
        c = x.size(1)
        D = x.size(2)

        I = I[:,:,0,:,:].view(1,1,1,D).float()
        J = J[:,:,0,:,:].view(1,1,D,1).float()
        x = x.view(s,1,c,D,D)
        M = M.view(s,g,4,1,1).float()

        # m0
        t3 = self.diff_h(I - M[:,:,0,:,:]) # s,g,1,D
        t2 = self.h(J + -1* M[:,:,1,:,:]) - self.h(J + -1*M[:,:,3,:]) #s,g,D,1
        t23 = torch.matmul(t2, t3) # s,g,D,D
        dL_dm0 = dL_dg * x * t23.view(s,g,1,D,D) # s,g,c,D,D
        dL_dm0 = dL_dm0.view(s,g,-1).sum(2) * -1 # s,g
        
        # m1
        t3 = self.diff_h(I - M[:,:,1,:,:])
        t2 = self.h(J - M[:,:,0,:,:] - self.h(J) - M[:,:,2,:,:])
        t23 = torch.matmul(t2, t3)
        dL_dm1 = dL_dg * x.view(s,1,c, D, D) * t23.view(s,g,1,D,D)
        dL_dm1 = dL_dm1.view(s,g,-1).sum(2) * -1
        
        # m2
        t3 = self.diff_h(I - M[:,:,2,:,:])
        t2 = self.h(J - M[:,:,1,:,:]) - self.h(J - M[:,:,3,:,:])
        t23 = torch.matmul(t2, t3)
        dL_dm2 = dL_dg * x.view(s,1,c, D, D) * t23.view(s,g,1,D,D)
        dL_dm2 = dL_dm2.view(s,g,-1).sum(2)
        
        # m3
        t3 = self.diff_h(I - M[:,:,3,:,:])
        t2 = self.h(J - M[:,:,0,:,:]) - self.h(J - M[:,:,2,:,:])
        t23 = torch.matmul(t2, t3)
        dL_dm3 = dL_dg * x.view(s,1,c, D, D) * t23.view(s,g,1,D,D)
        dL_dm3 = dL_dm3.view(s,g,-1).sum(2)
        
        dL_dm = torch.stack([dL_dm0, dL_dm1, dL_dm2, dL_dm3], dim=2)
        dL_dm = dL_dm * 500
        #print('Max Post:', torch.max(dL_dm), "Min Post: ", torch.min(dL_dm))
        """
        dL_dm = torch.zeros(s, g, 4) # output
        #if True:#use_gpu: 
        #    dL_dm = dL_dm.cuda()

        for s_i in range(s): # loop over samples
            s1 = time.time()
            for g_i in range(g): # loop over each glimpse
                s2 = time.time()
                for c in range(x.size(1)): # loop over channels
                    s3 = time.time()
                    for i in range(x.size(2)): 
                        for j in range(x.size(3)):
                            #print "S_N: ", s_i, "G_N", g_i, "C_N", c, "I: ", i, "J: ", j
                            t_1 = x[s_i, c, i, j]
                            t_2 = self.h_b(j-m[s_i, g_i, 1]) - self.h_b(j-m[s_i, g_i, 3])
                            t_3 = -1 * self.diff_h(i-m[s_i, g_i, 0])
                            dg = t_1 * t_2 * t_3
                            dL_dm[s_i, g_i, 0] = dL_dm[s_i, g_i, 0] + dL_dg[s_i, g_i, c, i, j] * dg

                            t_2 = self.h_b(i-m[s_i, g_i, 0]) - self.h_b(i-m[s_i, g_i, 2])
                            t_3 = -1 * self.diff_h(j-m[s_i, g_i, 1])
                            dg = t_1 * t_2 * t_3
                            dL_dm[s_i, g_i, 1] = dL_dm[s_i, g_i, 1] + dL_dg[s_i, g_i, c, i, j] * dg 

                            t_2 = self.h_b(j-m[s_i, g_i, 1]) - self.h_b(j-m[s_i, g_i, 3])
                            t_3 = self.diff_h(i-m[s_i, g_i, 2])
                            dg = t_1 * t_2 * t_3
                            dL_dm[s_i, g_i, 2] = dL_dm[s_i, g_i, 2] + dL_dg[s_i, g_i, c, i, j] * dg

                            t_2 = self.h_b(i-m[s_i, g_i, 0]) - self.h_b(i-m[s_i, g_i, 2])
                            t_3 = self.diff_h(i-m[s_i, g_i, 3])
                            dg = t_1 * t_2 * t_3
                            dL_dm[s_i, g_i, 3] = dL_dm[s_i, g_i, 3] + dL_dg[s_i, g_i, c, i, j] * dg
                    print("Channel: ", c, "glimpse: ", g_i, "sample: ", s_i, "took ", (time.time() - s3), "seconds")
                print("Glimpse: ", g_i, "sample: ", s_i, "took", (time.time() - s2), "seconds")
            print("Sample: ", s_i, "took ", (time.time() - s1), "seconds")
        """
        # x, m, f1, f2, use_gpu
        return None, dL_dm, None, None, None

class BoxCar(nn.Module):
    def __init__(self, ch=3, dim1=299, dim2=299, use_gpu=True):
        super(BoxCar, self).__init__()
        f1 = torch.from_numpy(np.arange(dim1)).view(1, 1, 1, dim1, 1)
        f2 = torch.from_numpy(np.arange(dim2)).view(1, 1, 1, 1, dim2)
        z1 = torch.zeros(ch*dim1).long().view(1, 1, ch, dim1, 1)
        z2 = torch.zeros(ch*dim2).long().view(1, 1, ch, 1, dim2)

        if use_gpu:
            f1 = Variable(f1.cuda())
            f2 = Variable(f2.cuda())
            z1 = Variable(z1.cuda())
            z2 = Variable(z2.cuda())
        else:
            f1 = Variable(f1)
            f2 = Variable(f2)
            z1 = Variable(z1)
            z2 = Variable(z2)

        f1.requires_grad = False
        f2.requires_grad = False
        z1.requires_grad = False
        z2.requires_grad = False

        self.f1 = f1 + z1
        self.f2 = f2 + z2
        if use_gpu:
            self.use_gpu = Variable(torch.Tensor(1)).cuda()
        else:
            self.use_gpu = Vairable(torch.Tensor(0))

    def forward(self, x, m):
        return BoxCarFunc()(x,m,self.f1,self.f2,self.use_gpu)


class BoxCarAuto(nn.Module):
    
    def __init__(self, ch=3, dim1=299, dim2=299, k=0.1, use_gpu=True):
        super(BoxCarAuto, self).__init__()
        f1 = torch.from_numpy(np.arange(dim1)).view(1, 1, 1, dim1, -1)
        f2 = torch.from_numpy(np.arange(dim2)).view(1, 1, 1, 1, -1)
        z1 = torch.zeros(ch*dim1).long().view(1, 1, ch, dim1, -1)
        z2 = torch.zeros(ch*dim2).long().view(1, 1, ch, 1, -1)

        if use_gpu:
            f1 = Variable(f1.cuda())
            f2 = Variable(f2.cuda())
            z1 = Variable(z1.cuda())
            z2 = Variable(z2.cuda())
        else:
            f1 = Variable(f1)
            f2 = Variable(f2)
            z1 = Variable(z1)
            z2 = Variable(z2)

        f1.requires_grad = False
        f2.requires_grad = False
        z1.requires_grad = False
        z2.requires_grad = False


        self.f1 = f1 + z1
        self.f2 = f2 + z2
        self.k = k
        self.ch = ch
        self.dim1 = dim1
        self.dim2 = dim2

    def logistic(self, x):
        return 1.0/(1 + torch.exp(-self.k * x))
    
    def forward(self, x, m):
        '''
            x -  s x ch x dim1 x dim2
            m - s x g x 4
            returns - s x g x ch x dim1 x dim2
        '''
        
        s = x.size(0)
        g = m.size(1)
        M = []
        for i in range(g):
            m1 = self.logistic((self.f1 + -1*m[:, i, 0].contiguous().view(s, 1, 1, 1, -1)).float())
            m2 = self.logistic((self.f1 + -1*m[:, i, 2].contiguous().view(s, 1, 1, 1, -1)).float())
            m3 = self.logistic((self.f2 + -1*m[:, i, 1].contiguous().view(s, 1, 1, 1, -1)).float())
            m4 = self.logistic((self.f2 + -1*m[:, i, 3].contiguous().view(s, 1, 1, 1, -1)).float())
            v = (m1 + -1*m2)*(m3 + -1*m4)
            M.append(v)

        M = torch.cat(M, 1)
        return x.view(s, 1, self.ch, self.dim1, -1) * M


class Upsampler(nn.Module):
    def __init__(self, set_zero=False, target_dim=(299,299), mode='bilinear'):

        super(Upsampler, self).__init__()
        self.h = target_dim[0]
        self.w = target_dim[1]
        self.set_zero = set_zero

        self.upsampler = torch.nn.Upsample(size=target_dim, mode=mode)
        
    def img_set_zero(self, x, tl_x, tl_y, br_x, br_y):
        """
        x: (3, 299, 299)
        """
        
        if tl_x > 0:
            x[:, :tl_x, :] = 0
        x[:, br_x:, :] = 0
        if tl_y > 0:
            x[:, :, :tl_y] = 0
        x[:, :, br_y:] = 0
        
        return x

    def img_crop(self, x, tl_x, tl_y, br_x, br_y, border_width=3, target_size=(299,299)):
        """
        Takes tensor of dimension x: (3, 299, 299) and
        f: (s, 4) containing tl_x, tl_y, br_x, br_y in that
        order. Returns upsampled crops
        """
        # note that the following step is not 
        # a part of the network, taking values
        # out of the tensor here
        tl_x, tl_y, br_x, br_y = int(tl_x.data[0]), int(tl_y.data[0]), int(br_x.data[0]), int(br_y.data[0])
        if self.set_zero:
            x = self.img_set_zero(x, tl_x, tl_y, br_x, br_y)

        # Add border to cropping to preserve gradient on boundaries
        tl_x = max(tl_x - border_width, 0)
        tl_y = max(tl_y - border_width, 0)
        br_x = min(br_x + border_width, x.size(1)-1)
        br_y = min(br_y + border_width, x.size(2)-1)
        
        cropped = x[:,tl_x:br_x,tl_y:br_y].contiguous()
        cropped = cropped.view(1, 3, cropped.size(1), cropped.size(2))
        upped = self.upsampler(cropped).view(3, 299, 299)
        return upped

    def img_crops(self, x, f):
        """
        x: (3, 299, 299)
        f: (g, 4) tl_x, tl_y, br_x, br_y
        returns cropped and upsampled same as x.size
        """
        out = []
        for i in range(f.size(0)):
            if self.set_zero:
                out.append(self.img_crop(x.clone(), f[i][0], f[i][1], f[i][2], f[i][3]))
            else:
                out.append(self.img_crop(x[i], f[i][0], f[i][1], f[i][2], f[i][3]))
        out = torch.stack(out, 0)
        return out

    def imgs_crops(self, x, f):
        """
        x: (s, g, 3, 299, 299)
        f: (s, g, 4) tl_x, tl_y, br_x, br_y
        returns cropped and upsampled same as x.size
        """
        out = []
        for i,x_i in enumerate(torch.unbind(x)):
            out.append(self.img_crops(x_i, f[i]))
        out = torch.stack(out, 0)
        return out

    def forward(self, x, f):
        return self.imgs_crops(x, f)

