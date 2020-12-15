###################################################################################################
#
# Copyright (C) 2020 Maxim Integrated Products, Inc. All Rights Reserved.
#
# Maxim Integrated Products, Inc. Default Copyright Notice:
# https://www.maximintegrated.com/en/aboutus/legal/copyrights.html
#
###################################################################################################
"""
Keyword spotting network for AI85/AI86
"""
import torch.nn as nn
import ai8x
import torch



class AI85KWS20Netv3(nn.Module):
    """
    E2E KWS with all Conv-1Ds
    """

    # num_classes = n keywords + 1 unknown
    def __init__(
            self,
            num_classes=21,
            num_channels=128,
            dimensions=(128, 1),  # pylint: disable=unused-argument
            fc_inputs=7,
            activation_k=1,
            activation_2=1,
            bias=False

    ):
        super().__init__()

        self.drop =  nn.Dropout(p=0.2)
############# T: 128 F :128
        self.voice_conv1 = ai8x.FusedConv1dReLU(num_channels, 100, 1, stride=1, padding=0,activation_k=activation_k,
                                                bias=bias)
############ T:  128 F: 100
        self.voice_conv2 = ai8x.FusedConv1dReLU(100, 96, 3, stride=1, padding=0,activation_k=activation_k,
                                                bias=bias)
###########  T: 126 F : 96
        self.voice_conv3 = ai8x.FusedMaxPoolConv1dReLU(96, 64, 3, stride=1, padding=1,activation_k=activation_k,
                                                bias=bias)
##########   T: 62 F : 64
        self.voice_conv4 = ai8x.FusedConv1dReLU(64, 48, 3, stride=1, padding=0,activation_k=activation_k,
                                                bias=bias)
##########  T : 60 F : 48
        self.kws_conv1 = ai8x.FusedMaxPoolConv1dReLU(48, 64, 3, stride=1, padding=1,activation_k=activation_2,
                                              bias=bias)
#########   T: 30 F : 64
        self.kws_conv2 = ai8x.FusedConv1dReLU(64, 96, 3, stride=1, padding=0,activation_k=activation_2,
                                              bias=bias)
#########   T: 28 F : 96
        self.kws_conv3 = ai8x.FusedAvgPoolConv1dReLU(96, 100, 3, stride=1, padding=1,activation_k=activation_2,
                                              bias=bias)
##########  T : 14 F: 100

        self.kws_conv4 = ai8x.FusedMaxPoolConv1dReLU(100, 64, 6, stride=1, padding=1,activation_k=activation_2,
                                              bias=bias)
#########   T : 2 F: 128

        self.fc = ai8x.Linear(256, num_classes, bias=bias)
        



    def forward(self, x):  # pylint: disable=arguments-differ
        # Run CNN
        x = self.voice_conv1(x)
        x = self.voice_conv2(x)
        x = self.drop(x)
        x = self.voice_conv3(x)
        x = self.voice_conv4(x)
        x = self.drop(x)
        x = self.kws_conv1(x)
        x = self.kws_conv2(x)
        x = self.drop(x)
        x = self.kws_conv3(x)
        x = self.kws_conv4(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)

        return x


def ai85kws20netv3(pretrained=False, **kwargs):
    """
    Constructs a AI85KWS20Net model.
    rn AI85KWS20Net(**kwargs)
    """
    assert not pretrained
    return AI85KWS20Netv3(**kwargs)


models = [
    {
        'name': 'ai85kws20netv3',
        'min_input': 1,
        'dim': 1,
    },
]
