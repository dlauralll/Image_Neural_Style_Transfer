import numpy as np
import tensorflow as tf
import tensorflow.contrib.eager as tfe
from tensorflow.python.keras.preprocessing import image as kp_image
from tensorflow.python.keras import models
from PIL import Image


class ImageConverter(object):

    def __init__(self, content_path, style_path, iteration=30, max_dim=512):
        self.content_path = content_path
        self.style_path = style_path
        self.iteration = iteration
        self.cur_iter = 0
        self.max_dim = max_dim
        self.content_layers = ['block5_conv2']
        self.style_layers = ['block1_conv1',
                             'block2_conv1',
                             'block3_conv1',
                             'block4_conv1',
                             'block5_conv1'
                             ]
        self.num_content_layers = len(self.content_layers)
        self.num_style_layers = len(self.style_layers)
        tf.compat.v1.enable_eager_execution()

    def _load_and_process_img(self, img_path):
        img = Image.open(img_path)
        long = max(img.size)
        scale = self.max_dim / long
        # return a resized copy of this image
        # Image.ANTIALIAS is a high-quality downsampling filter
        img = img.resize((round(img.size[0] * scale), round(img.size[1] * scale)), Image.ANTIALIAS)
        img = kp_image.img_to_array(img)

        # We need to broadcast the image array such that it has a batch dimension
        # eg. content = load_img(content_path).astype('uint8')
        img = np.expand_dims(img, axis=0)
        img = tf.keras.applications.vgg19.preprocess_input(img)
        return img

    @staticmethod
    def _deprocess_img(processed_img):
        x = processed_img.copy()
        if len(x.shape) == 4:
            x = np.squeeze(x, 0)
        assert len(x.shape) == 3, ("Input to de-process image must be an image of "
                                   "dimension [1, height, width, channel] or [height, width, channel]")
        if len(x.shape) != 3:
            raise ValueError("Invalid input to de-process image")
        # perform the inverse of the pre-processing step
        x[:, :, 0] += 103.939
        x[:, :, 1] += 116.779
        x[:, :, 2] += 123.68
        x = x[:, :, ::-1]
        x = np.clip(x, 0, 255).astype('uint8')
        return x

    def _get_model(self):
        """ Creates our model with access to intermediate layers.

        This function will load the VGG19 model and access the intermediate layers.
        These layers will then be used to create a new model that will take input image
        and return the outputs from these intermediate layers from the VGG model.

        Returns:
          returns a keras model that takes image inputs and outputs the style and
            content intermediate layers.
        """

        # Load our model. We load pretrained VGG, trained on imagenet data
        vgg = tf.keras.applications.vgg19.VGG19(include_top=False, weights='imagenet')
        vgg.trainable = False
        # Get output layers corresponding to style and content layers
        style_outputs = [vgg.get_layer(name).output for name in self.style_layers]
        content_outputs = [vgg.get_layer(name).output for name in self.content_layers]
        model_outputs = style_outputs + content_outputs
        # Build model
        return models.Model(vgg.input, model_outputs)

    # content loss: pass the network both the desired content image and our base input image
    def content_loss(self, base_content, target):
        return tf.reduce_mean(tf.square(base_content - target))

    # style loss: network the base input image and the style image
    def gram_matrix(self, input_tensor):
        # We make the image channels first
        channels = int(input_tensor.shape[-1])
        a = tf.reshape(input_tensor, [-1, channels])
        n = tf.shape(a)[0]
        gram = tf.matmul(a, a, transpose_a=True)
        return gram / tf.cast(n, tf.float32)

    def style_loss(self, base_style, gram_target):
        """Expects two images of dimension h, w, c"""
        # height, width, num filters of each layer
        # We scale the loss at a given layer by the size of the feature map and the number of filters
        # height, width, channels = base_style.get_shape().as_list()
        gram_style = self.gram_matrix(base_style)
        # / (4. * (channels ** 2) * (width * height) ** 2)
        return tf.reduce_mean(tf.square(gram_style - gram_target))

    # run gradient descent
    def run_gradient_descent(self, model, content_path, style_path):
        """Helper function to compute our content and style feature representations.

        This function will simply load and preprocess both the content and style
        images from their path. Then it will feed them through the network to obtain
        the outputs of the intermediate layers.

        Arguments:
          model: The model that we are using.
          content_path: The path to the content image.
          style_path: The path to the style image

        Returns:
          returns the style features and the content features.
        """
        # Load our images in
        content_image = self._load_and_process_img(content_path)
        style_image = self._load_and_process_img(style_path)
        # batch compute content and style features
        style_outputs = model(style_image)
        content_outputs = model(content_image)

        # Get the style and content feature representations from our model
        style_features = [style_layer[0] for style_layer in style_outputs[:self.num_style_layers]]
        content_features = [content_layer[0] for content_layer in content_outputs[self.num_style_layers:]]
        return style_features, content_features

    # compute the loss and gradients
    def compute_loss(self, model, loss_weights, init_image, gram_style_features, content_features):
        style_weight, content_weight = loss_weights
        model_outputs = model(init_image)

        style_output_features = model_outputs[:self.num_style_layers]
        content_output_features = model_outputs[self.num_style_layers:]

        style_score = 0
        content_score = 0

        # Accumulate style losses from all layers
        # Here, we equally weight each contribution of each loss layer
        weight_per_style_layer = 1.0 / float(self.num_style_layers)
        for target_style, comb_style in zip(gram_style_features, style_output_features):
            style_score += weight_per_style_layer * self.style_loss(comb_style[0], target_style)

        # Accumulate content losses from all layers
        weight_per_content_layer = 1.0 / float(self.num_content_layers)
        for target_content, comb_content in zip(content_features, content_output_features):
            content_score += weight_per_content_layer * self.content_loss(comb_content[0], target_content)

        style_score *= style_weight
        content_score *= content_weight

        # Get total loss
        loss = style_score + content_score
        return loss, style_score, content_score

    def compute_grads(self, cfg):
        with tf.GradientTape() as tape:
            all_loss = self.compute_loss(**cfg)
        # Compute gradients wrt input image
        total_loss = all_loss[0]
        return tape.gradient(total_loss, cfg['init_image']), all_loss

    # optimizing loop
    def run_style_transfer(self,
                           content_weight=1e3,
                           style_weight=1e-2):
        model = self._get_model()
        for layer in model.layers:
            layer.trainable = False

        # Get the style and content feature representations (from our specified intermediate layers)
        style_features, content_features = self.run_gradient_descent(model, self.content_path, self.style_path)
        gram_style_features = [self.gram_matrix(style_feature) for style_feature in style_features]

        # Set initial image
        init_image = self._load_and_process_img(self.content_path)
        init_image = tfe.Variable(init_image, dtype=tf.float32)
        # Create our optimizer
        opt = tf.compat.v1.train.AdamOptimizer(learning_rate=5, beta1=0.99, epsilon=1e-1)
        # Store our best result
        best_loss, best_img = float('inf'), None
        loss_weights = (style_weight, content_weight)
        cfg = {
            'model': model,
            'loss_weights': loss_weights,
            'init_image': init_image,
            'gram_style_features': gram_style_features,
            'content_features': content_features
        }

        # For displaying
        num_rows = 2
        num_cols = 5
        display_interval = self.iteration / (num_rows * num_cols)
        norm_means = np.array([103.939, 116.779, 123.68])
        min_vals = -norm_means
        max_vals = 255 - norm_means

        imgs = []
        for i in range(self.iteration):
            self.cur_iter = i
            print("Cur iteration: {}".format(str(i)))
            grads, all_loss = self.compute_grads(cfg)
            loss, style_score, content_score = all_loss
            opt.apply_gradients([(grads, init_image)])
            clipped = tf.clip_by_value(init_image, min_vals, max_vals)
            init_image.assign(clipped)

            if loss < best_loss:
                # Update best loss and best image from total loss.
                best_loss = loss
                best_img = ImageConverter._deprocess_img(init_image.numpy())

            if i % display_interval == 0:
                # Use the .numpy() method to get the concrete numpy array
                plot_img = init_image.numpy()
                plot_img = ImageConverter._deprocess_img(plot_img)
                imgs.append(plot_img)

        return best_img
