"""
Module to generate toy1 dataset.
"""

from __future__ import print_function
import argparse
import os
import shutil
import random
import string
import xml.etree.ElementTree as ET
from hier2hier.dataset.randomXml import getId

generatorArgsDefaults = {
   "max_len": 10,
   "tagPoolSize": 30,
}

def addArguments(parser, defaultArgs):
    parser.add_argument('--max-len', help="Max sequence length", default=defaultArgs.max_len)
    parser.add_argument('--tagPoolSize', help="Tag pool size.", default=defaultArgs.tagPoolSize)
    return parser

def postProcessArguments(args):
    return args

def generateCommon(appConfig, generatorArgs):
    return [getId((1, generatorArgs.max_len)) for _ in range(generatorArgs.tagPoolSize)]

def generateSample(generatorArgs, tagPool):
    """
    Generates input and output XML files for toy1 dataset.
    """
    tag1 = random.choice(tagPool)
    retval = ET.Element(tag1)

    tag2 = random.choice(tagPool)
    retval.append(ET.Element(tag2))
    return ET.ElementTree(retval)

def transformSample(xmlTree):
    root = xmlTree.getroot()
    child = root[0]
    root.remove(child)
    child.append(root)
    xmlTree._setroot(child)
    return ET.ElementTree(child)

