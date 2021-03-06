# encoding:utf-8
import numpy as np
import struct
import os
import time
from numpy.lib.stride_tricks import as_strided


class ConvolutionalLayer(object):
    def __init__(self, kernel_size, channel_in, channel_out, padding, stride, type=0):
        self.kernel_size = kernel_size
        self.channel_in = channel_in
        self.channel_out = channel_out
        self.padding = padding
        self.stride = stride
        self.forward = self.forward_raw
        self.backward = self.backward_raw
        if type == 1:  # type 设为 1 时，使用优化后的 foward 和 backward 函数
            self.forward = self.forward_speedup
            self.backward = self.backward_speedup
        print('\tConvolutional layer with kernel size %d, input channel %d, output channel %d.' % (
        self.kernel_size, self.channel_in, self.channel_out))

    def init_param(self, std=0.01):
        self.weight = np.random.normal(loc=0.0, scale=std,
                                       size=(self.channel_in, self.kernel_size, self.kernel_size, self.channel_out))
        self.bias = np.zeros([self.channel_out])

    def forward_raw(self, input):
        start_time = time.time()
        self.input = input  # [N, C, H, W]
        height = self.input.shape[2] + self.padding * 2
        width = self.input.shape[3] + self.padding * 2
        self.input_pad = np.zeros([self.input.shape[0], self.input.shape[1], height, width])
        self.input_pad[:, :, self.padding:self.padding + self.input.shape[2],
        self.padding:self.padding + self.input.shape[3]] = self.input
        height_out = (height - self.kernel_size) / self.stride + 1
        width_out = (width - self.kernel_size) / self.stride + 1
        self.output = np.zeros([self.input.shape[0], self.channel_out, height_out, width_out])
        for idxn in range(self.input.shape[0]):
            for idxc in range(self.channel_out):
                for idxh in range(height_out):
                    for idxw in range(width_out):
                        # TODO: 计算卷积层的前向传播，特征图与卷积核的内积再加偏置
                        weight_temp = self.weight[:, 0:self.kernel_size, 0:self.kernel_size, idxc]
                        input_temp = self.input_pad[idxn, :, idxh * self.stride:idxh * self.stride + self.kernel_size,
                                     idxw * self.stride:idxw * self.stride + self.kernel_size]
                        self.output[idxn, idxc, idxh, idxw] = np.sum(input_temp * weight_temp) + self.bias[
                            idxc]  # _______________________
        self.forward_time = time.time() - start_time
        return self.output

    def _padding(self, input):
        height = input.shape[2] + self.padding * 2
        width = input.shape[3] + self.padding * 2
        input_pad = np.zeros([input.shape[0], input.shape[1], height, width])
        input_pad[:, :, self.padding:self.padding + input.shape[2], self.padding:self.padding + input.shape[3]] = input
        return input_pad

    def split_by_strides(self, input, s):
        # s is convolutional stride
        N, C, H, W = input.shape
        oh = (H - self.kernel_size) // s + 1
        ow = (W - self.kernel_size) // s + 1
        shape = (N, C, oh, ow, self.kernel_size, self.kernel_size)
        strides = (input.strides[0], input.strides[1], input.strides[-2] * s,
                   input.strides[-1] * s, input.strides[-2], input.strides[-1])
        output = as_strided(input, shape=shape, strides=strides)  # (N, C, oh, ow, k, k)
        return output

    def forward_speedup(self, input):
        start_time = time.time()

        self.input = input
        input_pad = self._padding(input)
        if self.kernel_size > 1:
            self.oh = input_pad.shape[1] - self.kernel_size + 1
            self.ow = input_pad.shape[2] - self.kernel_size + 1
        self.input_split = self.split_by_strides(input_pad, self.stride)  # (N, cin, oh, ow, k, k)
        output = np.einsum('bchwij,cijo->bhwo', self.input_split, self.weight, optimize=True) + self.bias
        self.output = output.transpose(0, 3, 1, 2)

        self.forward_time = time.time() - start_time
        return self.output

    def backward_speedup(self, top_diff):
        # TODO: 改进backward函数，使得计算加速
        start_time = time.time()

        N, H, W = self.input.shape[0], self.input.shape[2], self.input.shape[3]
        temp_input_split = self.input_split.transpose(1, 4, 5, 0, 2, 3)  # (cin, k, k, N, oh, ow)
        temp_input_split = temp_input_split.reshape([self.channel_in * self.kernel_size * self.kernel_size, -1])
        temp_top_diff = top_diff.transpose(0, 2, 3, 1).reshape([-1, self.channel_out])
        self.d_weight = temp_input_split.dot(temp_top_diff)
        self.d_weight = self.d_weight.reshape([self.channel_in, self.kernel_size, self.kernel_size, self.channel_out])
        self.d_bias = top_diff.sum(axis=0).sum(axis=-1).sum(axis=-1)
        if self.stride > 1:
            temp = np.zeros((top_diff.shape[0], top_diff.shape[1], self.oh, self.ow))  # original input size
            temp[:, :, ::self.stride, ::self.stride] = top_diff
            top_diff = temp

        pad_diff = self._padding(top_diff)
        pad_diff = self.split_by_strides(pad_diff, 1)  # (N, cout, H, W, k, k)
        temp_kernel = self.weight[:, ::-1, ::-1, :]  # (cin, k, k, cout)
        temp_kernel = temp_kernel.transpose(3, 1, 2, 0).reshape([-1, self.channel_in])  # (cout * k * k, cin)
        pad_diff = pad_diff.transpose(0, 2, 3, 1, 4, 5).reshape(
            [-1, self.channel_out * self.kernel_size * self.kernel_size])  # (N * H * W, cout * k * k)
        bottom_diff = pad_diff.dot(temp_kernel).reshape([N, H, W, self.channel_in]).transpose(0, 3, 1, 2)

        self.backward_time = time.time() - start_time
        return bottom_diff

    def backward_raw(self, top_diff):
        start_time = time.time()
        self.d_weight = np.zeros(self.weight.shape)
        self.d_bias = np.zeros(self.bias.shape)
        bottom_diff = np.zeros(self.input_pad.shape)
        for idxn in range(top_diff.shape[0]):
            for idxc in range(top_diff.shape[1]):
                for idxh in range(top_diff.shape[2]):
                    for idxw in range(top_diff.shape[3]):
                        # TODO： 计算卷积层的反向传播， 权重、偏置的梯度和本层损失
                        '''intput_temp = self.input_pad[0:idxn, 0:idxc, idxh*self.stride:idxh*self.stride+self.kernel_size, idxw*self.stride:idxw*self.stride+self.kernel_size]
                        self.d_weight[:, :, :, idxc] += np.sum(top_diff * intput_temp)
                        self.d_bias[idxc] += top_diff[idxn, idxc, idxh, idxw]
                        bottom_diff[idxn, :, idxh*self.stride:idxh*self.stride+self.kernel_size, idxw*self.stride:idxw*self.stride+self.kernel_size] += intput_temp*self.weight
                         '''
                        self.d_weight[:, :, :, idxc] += top_diff[idxn, idxc, idxh, idxw] * self.input_pad[idxn, :,
                                                                                           idxh:idxh + self.kernel_size,
                                                                                           idxw:idxw + self.kernel_size]
                        self.d_bias[idxc] += top_diff[idxn, idxc, idxh, idxw]
                        bottom_diff[idxn, :, idxh * self.stride:idxh * self.stride + self.kernel_size,
                        idxw * self.stride:idxw * self.stride + self.kernel_size] += top_diff[
                                                                                         idxn, idxc, idxh, idxw] * self.weight[
                                                                                                                   :,
                                                                                                                   0:self.kernel_size,
                                                                                                                   0:self.kernel_size,
                                                                                                                   idxc]
        bottom_diff = bottom_diff[:, :, self.padding:self.padding + self.input.shape[2],
                      self.padding:self.padding + self.input.shape[3]]
        self.backward_time = time.time() - start_time
        return bottom_diff

    def get_gradient(self):
        return self.d_weight, self.d_bias

    def update_param(self, lr):
        self.weight += - lr * self.d_weight
        self.bias += - lr * self.d_bias

    def load_param(self, weight, bias):
        assert self.weight.shape == weight.shape
        assert self.bias.shape == bias.shape
        self.weight = weight
        self.bias = bias

    def get_forward_time(self):
        return self.forward_time

    def get_backward_time(self):
        return self.backward_time


class MaxPoolingLayer(object):
    def __init__(self, kernel_size, stride, type=0):
        self.kernel_size = kernel_size
        self.stride = stride
        self.forward = self.forward_raw
        self.backward = self.backward_raw_book
        if type == 1:  # type 设为 1 时，使用优化后的 foward 和 backward 函数
            self.forward = self.forward_speedup
            self.backward = self.backward_speedup
        print('\tMax pooling layer with kernel size %d, stride %d.' % (self.kernel_size, self.stride))

    def forward_raw(self, input):
        start_time = time.time()
        self.input = input  # [N, C, H, W]
        self.max_index = np.zeros(self.input.shape)
        height_out = (self.input.shape[2] - self.kernel_size) / self.stride + 1
        width_out = (self.input.shape[3] - self.kernel_size) / self.stride + 1
        self.output = np.zeros([self.input.shape[0], self.input.shape[1], height_out, width_out])
        for idxn in range(self.input.shape[0]):
            for idxc in range(self.input.shape[1]):
                for idxh in range(height_out):
                    for idxw in range(width_out):
                        # TODO： 计算最大池化层的前向传播， 取池化窗口内的最大值
                        self.output[idxn, idxc, idxh, idxw] = np.max(
                            self.input[idxn, idxc, idxh * self.stride:idxh * self.stride + self.kernel_size,
                            idxw * self.stride:idxw * self.stride + self.kernel_size])
                        curren_max_index = np.argmax(
                            self.input[idxn, idxc, idxh * self.stride:idxh * self.stride + self.kernel_size,
                            idxw * self.stride:idxw * self.stride + self.kernel_size])
                        curren_max_index = np.unravel_index(curren_max_index, [self.kernel_size, self.kernel_size])
                        self.max_index[
                            idxn, idxc, idxh * self.stride + curren_max_index[0], idxw * self.stride + curren_max_index[
                                1]] = 1
        return self.output

    def forward_speedup(self, input):
        # TODO: 改进forward函数，使得计算加速
        self.input = input  # [N, C, H, W]
        self.max_index = np.zeros(self.input.shape)
        height_out = (self.input.shape[2] - self.kernel_size) / self.stride + 1
        width_out = (self.input.shape[3] - self.kernel_size) / self.stride + 1
        self.output = np.zeros([self.input.shape[0], self.input.shape[1], height_out, width_out])
        for idxh in range(height_out):
            for idxw in range(width_out):
                # TODO： 计算最大池化层的前向传播， 取池化窗口内的最大值
                x_masked = self.input[:, :, idxh * self.stride:idxh * self.stride + self.kernel_size,
                            idxw * self.stride:idxw * self.stride + self.kernel_size]
                self.output[:, :, idxh, idxw] = np.max(x_masked, axis=(2, 3))
        return self.output

    def backward_speedup(self, top_diff):
        # TODO: 改进backward函数，使得计算加速
        bottom_diff = np.zeros(self.input.shape)
        for idxn in range(top_diff.shape[0]):
            for idxc in range(top_diff.shape[1]):
                for idxh in range(top_diff.shape[2]):
                    for idxw in range(top_diff.shape[3]):
                        # TODO: 最大池化层的反向传播， 计算池化窗口中最大值位置， 并传递损失
                        input_temp = self.input[idxn, idxc, idxh * self.stride:idxh * self.stride + self.kernel_size,
                                     idxw * self.stride:idxw * self.stride + self.kernel_size]
                        max_index = np.unravel_index(np.argmax(input_temp), input_temp.shape)
                        bottom_diff[idxn, idxc, idxh * self.stride + max_index[0], idxw * self.stride + max_index[1]] = \
                            top_diff[idxn, idxc, idxh, idxw]
        return bottom_diff

    def backward_raw_book(self, top_diff):
        bottom_diff = np.zeros(self.input.shape)
        for idxn in range(top_diff.shape[0]):
            for idxc in range(top_diff.shape[1]):
                for idxh in range(top_diff.shape[2]):
                    for idxw in range(top_diff.shape[3]):
                        # TODO: 最大池化层的反向传播， 计算池化窗口中最大值位置， 并传递损失
                        input_temp = self.input[idxn, idxc, idxh * self.stride:idxh * self.stride + self.kernel_size,
                                     idxw * self.stride:idxw * self.stride + self.kernel_size]
                        max_index = np.unravel_index(np.argmax(input_temp), input_temp.shape)
                        bottom_diff[idxn, idxc, idxh * self.stride + max_index[0], idxw * self.stride + max_index[1]] = \
                        top_diff[idxn, idxc, idxh, idxw]
        return bottom_diff


class FlattenLayer(object):
    def __init__(self, input_shape, output_shape):
        self.input_shape = input_shape
        self.output_shape = output_shape
        assert np.prod(self.input_shape) == np.prod(self.output_shape)
        print('\tFlatten layer with input shape %s, output shape %s.' % (str(self.input_shape), str(self.output_shape)))

    def forward(self, input):
        assert list(input.shape[1:]) == list(self.input_shape)
        # matconvnet feature map dim: [N, height, width, channel]
        # ours feature map dim: [N, channel, height, width]
        self.input = np.transpose(input, [0, 2, 3, 1])
        self.output = self.input.reshape([self.input.shape[0]] + list(self.output_shape))
        return self.output

    def backward(self, top_diff):
        assert list(top_diff.shape[1:]) == list(self.output_shape)
        top_diff = np.transpose(top_diff, [0, 3, 1, 2])
        bottom_diff = top_diff.reshape([top_diff.shape[0]] + list(self.input_shape))
        return bottom_diff
