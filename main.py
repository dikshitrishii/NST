from flask import Flask, request, send_file, send_from_directory
from flask_cors import CORS
import torch
from torchvision.models import vgg19
from PIL import Image
import io
import torchvision.transforms as transforms
import torch.nn.functional as F
import torch.optim as optim
import torch.nn as nn

app = Flask(__name__)
CORS(app)

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Image loader and transformer
imsize = 256
loader = transforms.Compose([
    transforms.Resize((imsize, imsize)),
    transforms.ToTensor()])


def load_image(image_bytes):
    image = Image.open(io.BytesIO(image_bytes))
    image = loader(image).unsqueeze(0)  # Add batch dimension
    return image.to(device, torch.float)


# Content loss definition
class ContentLoss(nn.Module):
    def __init__(self, target):
        super(ContentLoss, self).__init__()
        self.target = target.detach()  # Detach the target to avoid gradient computation

    def forward(self, input):
        self.loss = F.mse_loss(input, self.target)
        return input


# Gram matrix computation for style loss
def gram_matrix(input):
    batch_size, num_features, height, width = input.size()
    features = input.view(batch_size * num_features, height * width)
    gram = torch.mm(features, features.t())
    return gram.div(batch_size * num_features * height * width)


# Style loss definition
class StyleLoss(nn.Module):
    def __init__(self, target_feature):
        super(StyleLoss, self).__init__()
        self.target = gram_matrix(target_feature).detach()

    def forward(self, input):
        G = gram_matrix(input)
        self.loss = F.mse_loss(G, self.target)
        return input


# VGG19 model initialization
cnn = vgg19(pretrained=True).features.to(device).eval()

# Normalization module
mean = torch.tensor([0.485, 0.456, 0.406]).to(device)
std = torch.tensor([0.229, 0.224, 0.225]).to(device)


class Normalization(nn.Module):
    def __init__(self, mean, std):
        super(Normalization, self).__init__()
        self.mean = torch.tensor(mean).view(-1, 1, 1)
        self.std = torch.tensor(std).view(-1, 1, 1)

    def forward(self, img):
        return (img - self.mean) / self.std


# Layers for style and content losses
content_layers = ['conv_3']
style_layers = ['conv_1', 'conv_2', 'conv_3', 'conv_4', 'conv_5']


# Function to create style transfer model with losses
def get_style_model_and_losses(cnn, mean, std, style_img, content_img,
                               content_layers=content_layers, style_layers=style_layers):
    normalization = Normalization(mean, std).to(device)
    content_losses = []
    style_losses = []
    model = nn.Sequential(normalization)
    i = 0  # increment every time we see a conv
    for layer in cnn.children():
        if isinstance(layer, nn.Conv2d):
            i += 1
            name = 'conv_{}'.format(i)
        elif isinstance(layer, nn.ReLU):
            name = 'relu_{}'.format(i)
            layer = nn.ReLU(inplace=False)
        elif isinstance(layer, nn.MaxPool2d):
            name = 'pool_{}'.format(i)
        elif isinstance(layer, nn.BatchNorm2d):
            name = 'bn_{}'.format(i)
        else:
            raise RuntimeError('Unrecognized layer: {}'.format(layer.__class__.__name__))

        model.add_module(name, layer)

        if name in content_layers:
            target = model(content_img).detach()
            content_loss = ContentLoss(target)
            model.add_module("content_loss_{}".format(i), content_loss)
            content_losses.append(content_loss)

        if name in style_layers:
            target_feature = model(style_img).detach()
            style_loss = StyleLoss(target_feature)
            model.add_module("style_loss_{}".format(i), style_loss)
            style_losses.append(style_loss)

    # Trim off the layers after the last content and style losses
    for i in range(len(model) - 1, -1, -1):
        if isinstance(model[i], ContentLoss) or isinstance(model[i], StyleLoss):
            break

    model = model[:(i + 1)]
    return model, style_losses, content_losses


# Initialize the input image for optimization
def get_input_optimizer(input_img):
    optimizer = optim.LBFGS([input_img.requires_grad_(True)])
    return optimizer


# Function to perform style transfer optimization
def run_style_transfer(cnn, mean, std, content_img, style_img, input_img, num_steps=100, style_weight=10000,
                       content_weight=0.001):
    model, style_losses, content_losses = get_style_model_and_losses(cnn, mean, std, style_img, content_img)

    optimizer = get_input_optimizer(input_img)

    print('Optimizing..')
    run = [0]
    while run[0] <= num_steps:

        def closure():
            input_img.data.clamp_(0, 1)

            optimizer.zero_grad()
            model(input_img)
            style_score = 0
            content_score = 0

            for sl in style_losses:
                style_score += sl.loss
            for cl in content_losses:
                content_score += cl.loss

            style_score *= style_weight
            content_score *= content_weight

            loss = style_score + content_score
            loss.backward()

            run[0] += 1
            if run[0] % 50 == 0:
                print("run {}:".format(run))
                print('Style Loss : {:4f} Content Loss: {:4f}'.format(
                    style_score.item(), content_score.item()))
                print()

            return style_score + content_score

        optimizer.step(closure)

    input_img.data.clamp_(0, 1)

    return input_img


@app.route('/')
def index():
    return send_from_directory('.', 'templates/index.html')


@app.route('/style_transfer', methods=['POST'])
def style_transfer():
    content_image = request.files['content']
    style_image = request.files['style']

    content_img = load_image(content_image.read())
    style_img = load_image(style_image.read())
    input_img = content_img.clone()

    output = run_style_transfer(cnn, mean, std, content_img, style_img, input_img)

    output_image = transforms.ToPILImage()(output.squeeze(0).cpu().detach())

    img_byte_arr = io.BytesIO()
    output_image.save(img_byte_arr, format='PNG')
    img_byte_arr.seek(0)

    return send_file(img_byte_arr, mimetype='image/png', as_attachment=True, download_name='stylized_image.png')


if __name__ == '__main__':
    app.run(debug=True)
