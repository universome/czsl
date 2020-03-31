import argparse
parser = argparse.ArgumentParser('Global flags parser')
parser.add_argument('--debug', action='store_true', help='Enable debug mode.')
DEBUG = parser.parse_known_args()[0].debug

POS_INF = float('inf')
NEG_INF = float('-inf')

INPUT_DIMS = {
    'resnet18_feat': 512,
    'resnet34_feat': 512,
    'resnet50_feat': 2048,
    'mnist_1d': 784,
    'resnet18_conv': 256,
    'resnet34_conv': 256,
    'resnet50_conv': 1024,
}
