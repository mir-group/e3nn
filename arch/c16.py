# pylint: disable=C,R,E1101
'''
Based on c15

+ backup
'''
import torch
import torch.nn as nn
from se3_cnn.convolution import SE3Convolution
from se3_cnn.batchnorm import SE3BatchNorm
from se3_cnn.non_linearities.scalar_activation import BiasRelu
from se3_cnn.non_linearities.tensor_product import TensorProduct
from se3_cnn import SO3
from util_cnn.model import Model
from util_cnn import time_logging
import logging
import numpy as np
import copy

logger = logging.getLogger("trainer")


class Block(nn.Module):
    def __init__(self, scalar_out, vector_out, scalar_in, vector_in, relu, size=7, padding=3, stride=1):
        super().__init__()
        self.tensor = TensorProduct([(scalar_in, 1, False), (vector_in, 3, True)]) if vector_in > 0 else None
        self.conv = SE3Convolution(size, 3,
            [(scalar_out, SO3.repr1), (vector_out, SO3.repr3)],
            [(scalar_in, SO3.repr1), (vector_in, SO3.repr3), (vector_in, SO3.repr3x3)],
            stride=stride,
            padding=padding)
        self.bn = SE3BatchNorm([(scalar_out, 1), (vector_out, 3)])
        self.relu = BiasRelu([(scalar_out, True), (vector_out * 3, False)]) if relu else None

    def forward(self, sv): # pylint: disable=W
        if self.tensor is not None:
            t = self.tensor(sv)
            svt = torch.cat([sv, t], dim=1)
        else:
            svt = sv
        sv = self.conv(svt) # only convolution
        sv = self.bn(sv)
        if self.relu is not None:
            sv = self.relu(sv)
        return sv

class CNN(nn.Module):

    def __init__(self, number_of_classes):
        super(CNN, self).__init__()

        logger.info("Create CNN for classify %d classes", number_of_classes)

        # Hout = (Hin+2∗padding−kernel_size) // stride + 1

        features = [(1, 0), # 64
            (4, 2), # 33
            (10, 5), # 33
            (10, 5), # 17
            (12, 6), # 17
            (12, 6), # 9
            (14, 7), # 9
            (number_of_classes, 0)]  # 5

        self.convolutions = []

        s = 64
        for i in range(len(features) - 1):
            relu = i < len(features) - 2
            stride = 2 if i % 2 == 0 else 1
            if stride == 2:
                size = 6 if s % 2 == 0 else 7
            else:
                size = 7
            padding = 3

            assert (s + 2 * padding - size) % stride == 0
            s = (s + 2 * padding - size) // stride + 1
            conv = Block(features[i + 1][0], features[i + 1][1], features[i][0], features[i][1], relu, size, padding, stride)
            setattr(self, 'conv{}'.format(i), conv)
            self.convolutions.append(conv)

        self.bn_in = nn.BatchNorm3d(1, affine=False)
        self.bn_out = nn.BatchNorm1d(number_of_classes, affine=True)

    def forward(self, x): # pylint: disable=W
        '''
        :param x: [batch, features, x, y, z]
        '''
        x = self.bn_in(x.contiguous())
        t = time_logging.start()
        for i, conv in enumerate(self.convolutions):
            x = conv(x)
            t = time_logging.end("block {}".format(i), t)

        # [batch, features]
        x = x.mean(-1).mean(-1).mean(-1)
        x = self.bn_out(x.contiguous())
        return x


class MyModel(Model):

    def __init__(self):
        super(MyModel, self).__init__()
        self.cnn = None
        self.optimizer = None

        self.previous_loss = None
        self.saved_state = None
        self.good_learning_rate = self.learning_rate = 1e-2
        self.true_epoch = 0

    def initialize(self, number_of_classes):
        self.cnn = CNN(number_of_classes)
        self.optimizer = torch.optim.Adam(self.cnn.parameters())

    def get_cnn(self):
        if self.cnn is None:
            raise ValueError("Need to call initialize first")
        return self.cnn

    def get_batch_size(self, epoch=None):
        return 16

    def get_optimizer(self):
        return self.optimizer

    def get_learning_rate(self, epoch):
        momentum = 0.5 * 0.5 ** self.true_epoch
        logger.info("Momentum is %.0e and learning rate is %.0e", momentum, self.learning_rate)

        for module in self.cnn.modules():
            if isinstance(module, SE3BatchNorm):
                module.momentum = momentum

        return self.learning_rate

    def training_done(self, avg_loss, accuracy):
        if self.previous_loss is not None and self.saved_state is not None:
            if avg_loss > 1.3 * self.previous_loss:
                # reject
                logger.info("rejected")
                self.learning_rate = self.good_learning_rate / 1.1414
                self.cnn.load_state_dict(self.saved_state[0])
                self.optimizer.load_state_dict(self.saved_state[1])
                return
        # accept
        self.true_epoch += 1

        if self.previous_loss is None:
            self.previous_loss = avg_loss
            self.saved_state = copy.deepcopy((self.cnn.state_dict(), self.optimizer.state_dict()))
            return

        if avg_loss < self.previous_loss:
            logger.info("accepted with improvement of %.1e", self.previous_loss - avg_loss)
            self.previous_loss = avg_loss
            self.saved_state = copy.deepcopy((self.cnn.state_dict(), self.optimizer.state_dict()))
            self.good_learning_rate = self.learning_rate

            if self.learning_rate < 0.1:
                self.learning_rate *= 1.189
        else:
            logger.info("accepted but not better")

    def load_files(self, files):
        images = np.array([np.load(file)['arr_0'] for file in files], dtype=np.float32)
        images = images.reshape((-1, 1, 64, 64, 64))
        images = (images - 0.02267) / 0.14885
        images = torch.FloatTensor(images)
        return images