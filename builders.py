from keras.models import Model
from keras.layers import Input, Conv2D, UpSampling2D, Conv2DTranspose
from keras.layers import GlobalAveragePooling2D, MaxPooling2D, concatenate
from keras.layers import LeakyReLU, BatchNormalization, Dense, Activation, Reshape

from typing import List # Type hints requires Python >= 3.5

# Local imports
from unet import unet_cell, LEAKY_RELU_ALPHA, BATCH_NORM_MOMENTUM

# Create a function for constructing the upscaler (aka generator)
def build_upscaler(inputs, # TODO: What type is this?
                   num_filters_in_layer: List[int]=[16, 32, 64],
                   num_cells_in_layer: List[int]=[3, 3, 3],
                   bottleneck_before_concat: bool=False) -> Model:
    """Build a model which upscales inputs by factor of 4 (i.e., 
       doubles the height and width of inputs)
    
    # Arguments
        inputs (Tensor): images to be upscaled; must have 
            data_format == 'channels_last' # TODO: Enable both formats
            
        num_filters_in_layer (list of ints): how many filters are 
            used in each layer. (Note: a "layer" consists of
            successive applications of stride=1 convolution; two 
            layers are separated by a pooling or upsampling operation. 
            The length of this list tells us how many layers we have.)
            
        num_cells_in_layer (list of ints): how many successive 
            applications of convolution + BN + activation are applied 
            before starting a new layer.
            
        bottleneck_before_concat (bool): whether or not to apply a 
            convolution with 1x1 kernels prior to concatenating 
            (doing so will reduce the total number of parameters 
            required by the upscaler)
    
    # Returns
        Keras Model which takes in an image tensor and returns 
        a tensor of upscaled images.
    """
    
    # Validate inputs
    if len(num_filters_in_layer) != len(num_cells_in_layer):
        raise ValueError("len(num_filters_in_layer) should equal "
            "len(num_cells_in_layer). Received lengths: "
            "({}, {})".format(len(num_filters_in_layer), len(num_cells_in_layer)))
    
    x = inputs
    x = UpSampling2D()(x)
    carry_forward_tensors = []

    # Zip the parameters that apply to the encoder (exclude last 
    # parameters for bottom layer)
    contraction_filters_and_cells = zip(num_filters_in_layer[:-1],
                                        num_cells_in_layer[:-1])

    # Encoder
    for i, (num_filters, num_cells) in enumerate(contraction_filters_and_cells):
        if i == 0:
            # First layer does not downsample
            for j in range(num_cells):
                x = unet_cell(x, num_filters=num_filters) 
                # Note: We make extensive use of default values.
        else:
            # Downsample first
            x = unet_cell(x, num_filters=num_filters, strides=2)
            for j in range(num_cells - 1):
                x = unet_cell(x, num_filters=num_filters)
        carry_forward_tensors.append(x)

    # Bottom layer
    num_filters_at_bottom = num_filters_in_layer[-1]
    num_cells_at_bottom = num_cells_in_layer[-1]

    x = unet_cell(x, num_filters=num_filters_at_bottom, strides=2)
    for i in range(num_cells_at_bottom - 1):
        x = unet_cell(x, num_filters=num_filters_at_bottom)

    # Decoder
    filters_cells_and_carrys = reversed(
        list(zip(num_filters_in_layer[:-1],
                 num_cells_in_layer[:-1],
                 carry_forward_tensors)))
    
    for num_filters, num_cells, carry in filters_cells_and_carrys:
        # First, we upsample the output of the previous layer
        # TODO: parametrize upsampling method (Conv2DTrans vs UpSample2D)
        x = unet_cell(x, transpose_conv=True, num_filters=num_filters, strides=2)
        if bottleneck_before_concat:
            x = unet_cell(x, num_filters=num_filters//2, kernel_size=1)
            carry = unet_cell(carry, num_filters=num_filters//2, kernel_size=1)
        x = concatenate([x, carry])
        # Then, we apply successive convolution
        for _ in range(num_cells - 1):
            x = unet_cell(x, num_filters=num_filters)

    # Output
    x = unet_cell(x, num_filters=1, kernel_size=1, activation='sigmoid',
                  batch_normalization=False)

    model = Model(inputs=inputs, outputs=x)
    return model

# TODO: Test keras.applications (ResNet, Xception, etc.) as discriminators
# Note: ResNet, Xception have *required* input dimensions larger than 
# something on the order of 100x100...
# Instead, consider implementing and using ResNet / Xception cells
def build_discriminator(low_res_input, high_res_input, # TODO: What type are these?
                        num_filters_in_layer: List[int]=[16, 32, 64],
                        num_cells_in_layer: List[int]=[3, 3, 3],
                        num_units_in_dense_layer: List[int]=[]):
    """Builds a model which classifies high_res_input as real
       or generated (where "real" -> 1, "generated" -> 0).
    
    # Arguments
        low_res_input (Tensor): low resolution images
        
        high_res_input (Tensor): upsampled versions of low_res_input
        
        # Note: both input tensors should have data_format == 'channels_last'
        # TODO: Enable both formats
        
        num_filters_in_layer (list of ints): how many filters are 
            used in each layer. (Note: a "layer" consists of
            successive applications of stride=1 convolution; two 
            layers are separated by a pooling operation. The length 
            of this list tells us how many layers we have.)
            
        num_cells_in_layer (list of ints): how many successive 
            applications of convolution + BN + activation are applied 
            before starting a new layer.
            
        num_units_in_dense_layer (list of ints): how many units are 
            used in each dense layer (Note: the length of this list 
            tells us how many dense layers we have before the final 
            classification layer.)
    
    # Returns
        Keras Model which takes in two image tensors (low_res_input, 
        high_res_input) and returns a scalar between [0, 1] which 
        represents how realistic high_res_input appears.
    """
    
    # Validate inputs
    if len(num_filters_in_layer) != len(num_cells_in_layer):
        raise ValueError("len(num_filters_in_layer) should equal "
            "len(num_cells_in_layer). Received lengths: "
            "({}, {})".format(len(num_filters_in_layer), len(num_cells_in_layer)))
    
    x = low_res_input
    x = UpSampling2D()(x)
    x = concatenate([x, high_res_input])
    
    filters_and_cells = zip(num_filters_in_layer, num_cells_in_layer)
    
    for i, (num_filters, num_cells) in enumerate(filters_and_cells):
        for j in range(num_cells):
            x = unet_cell(x, num_filters=num_filters) # TODO: Rename `unet_cell`
        x = MaxPooling2D(pool_size=2)(x)
    
    x = GlobalAveragePooling2D()(x)
    x = LeakyReLU(alpha=LEAKY_RELU_ALPHA)(x)
    x = BatchNormalization(momentum=BATCH_NORM_MOMENTUM)(x)
    
    for num_units in num_units_in_dense_layer:
        x = Dense(units=num_units)(x)
        x = LeakyReLU(alpha=LEAKY_RELU_ALPHA)(x)
        x = BatchNormalization(momentum=BATCH_NORM_MOMENTUM)(x)
    
    validity = Dense(units=1, activation='sigmoid')(x)

    model = Model(inputs=[low_res_input, high_res_input], outputs=validity)
    return model