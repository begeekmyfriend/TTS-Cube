import torch
import tqdm
from torch import nn
from models.clarinet.modules import Conv, ResBlock
from models.clarinet.loss import sample_from_gaussian


class Wavenet(nn.Module):
    def __init__(self, out_channels=1, num_blocks=3, num_layers=10,
                 residual_channels=512, gate_channels=512, skip_channels=512,
                 kernel_size=2, cin_channels=128,
                 upsample_scales=None, causal=True):
        super(Wavenet, self).__init__()

        self.causal = causal
        self.num_blocks = num_blocks
        self.num_layers = num_layers
        self.out_channels = out_channels
        self.gate_channels = gate_channels
        self.residual_channels = residual_channels
        self.skip_channels = skip_channels
        self.cin_channels = cin_channels
        self.kernel_size = kernel_size

        self.front_channels = 32
        self.front_conv = nn.Sequential(
            Conv(1, self.residual_channels, self.front_channels, causal=self.causal),
            nn.ReLU()
        )

        self.res_blocks = nn.ModuleList()
        for b in range(self.num_blocks):
            for n in range(self.num_layers):
                self.res_blocks.append(ResBlock(self.residual_channels, self.gate_channels, self.skip_channels,
                                                self.kernel_size, dilation=self.kernel_size ** n,
                                                cin_channels=self.cin_channels, local_conditioning=True,
                                                causal=self.causal, mode='SAME'))

        self.final_conv = nn.Sequential(
            nn.ReLU(),
            Conv(self.skip_channels, self.skip_channels, 1, causal=self.causal),
            nn.ReLU(),
            Conv(self.skip_channels, self.out_channels, 1, causal=self.causal)
        )

        self.upsample_conv = nn.ModuleList()
        for s in upsample_scales:
            convt = nn.ConvTranspose2d(1, 1, (3, 2 * s), padding=(1, s // 2), stride=(1, s))
            convt = nn.utils.weight_norm(convt)
            nn.init.kaiming_normal_(convt.weight)
            self.upsample_conv.append(convt)
            self.upsample_conv.append(nn.LeakyReLU(0.4))

    def forward(self, x, c):
        c = self.upsample(c)
        out = self.wavenet(x, c)
        return out

    def generate(self, num_samples, c=None, device='cpu'):
        # Only a waveform generation
        # from ipdb import set_trace
        # set_trace()
        x = torch.zeros(1, 1, num_samples + 1)
        c = self.upsample(c)
        for i in tqdm.tqdm(range(num_samples)):
            if i >= self.receptive_field_size():
                start_idx = i - self.receptive_field_size() + 1
            else:
                start_idx = 0
            x_in = x[:, :, start_idx:i + 1].to(device)
            if c is not None:
                cond = c[:, :, start_idx:i + 1]
            else:
                cond = None
            out = self.wavenet(x_in, cond)
            # sampling input
            x[:, :, i + 1] = sample_from_gaussian(out[:, :, -1:]).to(torch.device("cpu"))
            del out, x_in, cond
        return x[:, :, 1:]

    def upsample(self, c):
        if self.upsample_conv is not None:
            # B x 1 x C x T'
            c = c.unsqueeze(1)
            for f in self.upsample_conv:
                c = f(c)
            # B x C x T
            c = c.squeeze(1)
        return c

    def wavenet(self, tensor, c=None):
        h = self.front_conv(tensor)
        skip = 0
        for i, f in enumerate(self.res_blocks):
            h, s = f(h, c)
            skip += s
        out = self.final_conv(skip)
        return out

    def receptive_field_size(self):
        num_dir = 1 if self.causal else 2
        dilations = [2 ** (i % self.num_layers) for i in range(self.num_layers * self.num_blocks)]
        return num_dir * (self.kernel_size - 1) * sum(dilations) + self.front_channels
