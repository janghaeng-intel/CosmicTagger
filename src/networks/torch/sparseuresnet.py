import torch
import torch.nn as nn
import sparseconvnet as scn


from src import utils
FLAGS = utils.flags.FLAGS()

'''UResNet is implemented recursively here.

On the downsample pass, each layer receives a tensor as input.
There are a series of convolutional blocks (either conv + BN + Relu or residual blocks)
and then a downsampling step.

After the downsampling step, the output goes into the next lowest layer.  The next lowest
layer performs it's steps (recursively down the network) and then returns an upsampled
image.  So, each layer returns an image of the same resolution as it's input.

On the upsampling pass, the layer recieves a downsampled image.  It performs a series of convolutions,
and merges across the downsampled layers either before or after the convolutions.

It then performs an upsampling step, and returns the upsampled tensor.

'''

class SparseBlock(nn.Module):

    def __init__(self, inplanes, outplanes, nplanes=1):

        nn.Module.__init__(self)

        self.conv1 = scn.SubmanifoldConvolution(dimension=3,
            nIn=inplanes,
            nOut=outplanes,
            filter_size=[nplanes,3,3],
            bias=False)

        if FLAGS.BATCH_NORM:
            self.bn1 = scn.BatchNormReLU(outplanes)
        self.relu = scn.ReLU()

    def forward(self, x):

        out = self.conv1(x)
        if FLAGS.BATCH_NORM:
            out = self.bn1(out)
        else:
            out = self.relu(out)

        return out



class SparseResidualBlock(nn.Module):

    def __init__(self, inplanes, outplanes, nplanes=1):
        nn.Module.__init__(self)


        self.conv1 = scn.SubmanifoldConvolution(dimension=3,
            nIn         = inplanes,
            nOut        = outplanes,
            filter_size = [nplanes,3,3],
            bias=False)


        if FLAGS.BATCH_NORM:
            self.bn1 = scn.BatchNormReLU(outplanes)

        self.conv2 = scn.SubmanifoldConvolution(dimension=3,
            nIn         = outplanes,
            nOut        = outplanes,
            filter_size = [nplanes,3,3],
            bias        = False)

        if FLAGS.BATCH_NORM:
            self.bn2 = scn.BatchNormalization(outplanes)

        self.residual = scn.Identity()
        self.relu = scn.ReLU()

        self.add = scn.AddTable()

    def forward(self, x):

        residual = self.residual(x)

        out = self.conv1(x)
        if FLAGS.BATCH_NORM:
            out = self.bn1(out)
        else:
            out = self.relu(out)
        out = self.conv2(out)

        if FLAGS.BATCH_NORM:
            out = self.bn2(out)

        # The addition of sparse tensors is not straightforward, since

        out = self.add([out, residual])

        out = self.relu(out)

        return out




class SparseConvolutionDownsample(nn.Module):

    def __init__(self, inplanes, outplanes,nplanes=1):
        nn.Module.__init__(self)

        self.conv = scn.Convolution(dimension=3,
            nIn             = inplanes,
            nOut            = outplanes,
            filter_size     = [nplanes,2,2],
            filter_stride   = [1,2,2],
            bias            = False
        )
        if FLAGS.BATCH_NORM:
            self.bn   = scn.BatchNormalization(outplanes)
        self.relu = scn.ReLU()

    def forward(self, x):
        out = self.conv(x)

        if FLAGS.BATCH_NORM:
            out = self.bn(out)

        out = self.relu(out)
        return out


class SparseConvolutionUpsample(nn.Module):

    def __init__(self, inplanes, outplanes, nplanes=1):
        nn.Module.__init__(self)

        self.conv = scn.Deconvolution(dimension=3,
            nIn             = inplanes,
            nOut            = outplanes,
            filter_size     = [nplanes,2,2],
            filter_stride   = [1,2,2],
            bias            = False
        )
        if FLAGS.BATCH_NORM:
            self.bn   = scn.BatchNormalization(outplanes)
        self.relu = scn.ReLU()

    def forward(self, x):
        out = self.conv(x)
        if FLAGS.BATCH_NORM:
            out = self.bn(out)
        out = self.relu(out)
        return out

class SparseBlockSeries(torch.nn.Module):


    def __init__(self, inplanes, n_blocks, n_planes=1, residual=False):
        torch.nn.Module.__init__(self)

        if residual:
            self.blocks = [ SparseResidualBlock(inplanes, inplanes, n_planes) for i in range(n_blocks) ]
        else:
            self.blocks = [ SparseBlock(inplanes, inplanes, n_planes) for i in range(n_blocks)]

        for i, block in enumerate(self.blocks):
            self.add_module('block_{}'.format(i), block)


    def forward(self, x):
        for i in range(len(self.blocks)):
            x = self.blocks[i](x)
        return x


class SparseDeepestBlock(nn.Module):

    def __init__(self, inplanes, n_blocks, residual):
        nn.Module.__init__(self)


        # The deepest block applies convolutions that act on all three planes together

        # First we apply a convolution to map all three planes into 1 plane (of the same spatial size)

        self.merger = scn.Convolution(dimension=3,
            nIn             = inplanes,
            nOut            = FLAGS.NPLANES*inplanes,
            filter_size     = [FLAGS.NPLANES,1,1],
            filter_stride   = [1,1,1],
            bias            = False)


        self.blocks = SparseBlockSeries(FLAGS.NPLANES*inplanes, FLAGS.RES_BLOCKS_DEEPEST_LAYER, n_planes=1, residual=residual)

        self.splitter = scn.Deconvolution(dimension=3,
            nIn             = FLAGS.NPLANES*inplanes,
            nOut            = inplanes,
            filter_size     = [FLAGS.NPLANES,1,1],
            filter_stride   = [1,1,1],
            bias            = False)


    def forward(self, x):
        x = self.merger(x)
        x = self.blocks(x)
        x = self.splitter(x)
        return x


class NoConnection(nn.Module):

    def __init__(self):
        nn.Module.__init__(self)

    def forward(self, x, residual):
        return x

class SumConnection(nn.Module):

    def __init__(self):
        nn.Module.__init__(self)
        self.op = scn.AddTable()

    def forward(self, x, residual):
        return self.op([x, residual])

class ConcatConnection(nn.Module):

    def __init__(self, inplanes):
        nn.Module.__init__(self)

        self.concat = scn.ConcatTable()
        self.bottleneck = scn.SubmanifoldConvolution(3,
                            nIn         = 2*inplanes,
                            nOut        = inplanes,
                            filter_size = 1,
                            bias        = False)

    def forward(self, x, residual):
        print(type(x))
        print(type(residual))


        x = self.concat([x, residual])
        return self.bottleneck(x)


class SparseUNetCore(nn.Module):

    def __init__(self, depth, nlayers, inplanes, residual):
        nn.Module.__init__(self)


        self.layers = nlayers
        self.depth  = depth

        if depth == 0:
            self.main_module = SparseDeepestBlock(inplanes, FLAGS.RES_BLOCKS_DEEPEST_LAYER, residual = residual)
        else:
            # Residual or convolutional blocks, applied in series:
            self.down_blocks = SparseBlockSeries(inplanes, nlayers,n_planes=1, residual=residual)

            if FLAGS.GROWTH_RATE == "linear":
                n_filters_next_layer = inplanes + FLAGS.N_INITIAL_FILTERS
            elif FLAGS.GROWTH_RATE == "multiplicative":
                n_filters_next_layer = inplanes * 2


            # Down sampling operation:
            self.downsample  = SparseConvolutionDownsample(inplanes, n_filters_next_layer)

            # Submodule:
            self.main_module = SparseUNetCore(depth-1, nlayers, n_filters_next_layer, residual = residual)
            # Upsampling operation:
            self.upsample    = SparseConvolutionUpsample(n_filters_next_layer, inplanes)


            # Convolutional or residual blocks for the upsampling pass:
            self.up_blocks = SparseBlockSeries(inplanes, nlayers,n_planes=1, residual=residual)

            # Residual connection operation:
            if FLAGS.CONNECTIONS == "sum":
                self.connection = SumConnection()
            elif FLAGS.CONNECTIONS == "concat":
                self.connection = ConcatConnection(inplanes)
            else:
                self.connection = NoConnection()


    def forward(self, x):


        # Take the input and apply the downward pass convolutions.  Save the residual
        # at the correct time.
        if self.depth != 0:
            if FLAGS.CONNECT_PRE_RES_BLOCKS_DOWN:
                residual = x

            x = self.down_blocks(x)

            if not FLAGS.CONNECT_PRE_RES_BLOCKS_DOWN:
                residual = x

            # perform the downsampling operation:
            x = self.downsample(x)

        # Apply the main module:
        x = self.main_module(x)

        if self.depth != 0:

            # perform the upsampling step:
            # perform the downsampling operation:
            x = self.upsample(x)

            # Connect with the residual if necessary:
            if FLAGS.CONNECT_PRE_RES_BLOCKS_UP:
                x = self.connection(x, residual)

            # Apply the convolutional steps:
            x = self.up_blocks(x)

            if not FLAGS.CONNECT_PRE_RES_BLOCKS_UP:
                x = self.connection(x, residual)

        return x





class UResNet(torch.nn.Module):

    def __init__(self, shape):
        torch.nn.Module.__init__(self)



        # Create the sparse input tensor:
        # (first spatial dim is plane)
        # self.input_tensor = scn.InputLayer(dimension=3, spatial_size=[FLAGS.NPLANES,640,1024])
        self.input_tensor = scn.InputLayer(dimension=3,
            spatial_size=[FLAGS.NPLANES,shape[0], shape[1]])


        self.initial_convolution = scn.SubmanifoldConvolution(dimension=3,
            nIn=1,
            nOut=FLAGS.N_INITIAL_FILTERS,
            filter_size=[1,3,3],
            bias=False)



        n_filters = FLAGS.N_INITIAL_FILTERS
        # Next, build out the convolution steps:

        self.net_core = SparseUNetCore(depth=FLAGS.NETWORK_DEPTH,
            nlayers=FLAGS.RES_BLOCKS_PER_LAYER,
            inplanes=FLAGS.N_INITIAL_FILTERS,
            residual=FLAGS.RESIDUAL)

        # We need final output shaping too.
        # Even with shared weights, keep this separate:



        self.final_layer = SparseBlockSeries(FLAGS.N_INITIAL_FILTERS, FLAGS.RES_BLOCKS_FINAL, residual=FLAGS.RESIDUAL)
        self.bottleneck  = scn.SubmanifoldConvolution(dimension=3,
            nIn=FLAGS.N_INITIAL_FILTERS,
            nOut=3,
            filter_size=1,
            bias=False)

        # The rest of the final operations (reshape, softmax) are computed in the forward pass

        # # Configure initialization:
        # for m in self.modules():
        #     if isinstance(m, scn.SubmanifoldConvolution):
        #         nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
        #     if isinstance(m, scn.Deconvolution) or isinstance(m, scn.Convolution):
        #         nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
        #     elif isinstance(m, scn.BatchNormalization):
        #         nn.init.constant_(m.weight, 1)
        #         nn.init.constant_(m.bias, 0)

        self._s_to_d_1 = scn.SparseToDense(dimension=3, nPlanes=1)
        self._s_to_d_3 = scn.SparseToDense(dimension=3, nPlanes=3)

    def convert_to_scn(self, _input):

        return self.input_tensor(_input)

    def sparse_to_dense(self, _input, nPlanes):

        if nPlanes == 1:
            return self._s_to_d_1(_input)
        else:
            return self._s_to_d_3(_input)

    def forward(self, _input):


        batch_size = _input[-1]
        x = self.input_tensor(_input)

        # Apply the initial convolutions:
        x = self.initial_convolution(x)

        # Apply the main unet architecture:
        x = self.net_core(x)

        # Apply the final residual block to each plane:
        x = self.final_layer(x)
        x = self.bottleneck(x)


        return x