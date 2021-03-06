"""
In regular seq2seq, information flows linearly from one sequence position to another.
In hier2hier, information flows linearly within text positions and then across the
XML connectivity graph.

This module implements information flow through connectivity graph connecting XML nodes
to their neighbors.
"""
from __future__ import unicode_literals, print_function, division
from io import open
from collections import OrderedDict
import unicodedata
import string
import re
import random

import torch
import torch.nn as nn
import torch.nn.utils.rnn as rnn
from torch import optim
import torch.nn.functional as F
from hier2hier.util import (onehotencode, checkNans, invertPermutation, blockProfiler,
                            methodProfiler, lastCallProfile)
from hier2hier.models.moduleBase import ModuleBase
from hier2hier.models.hier2hierBatch import splitByToi

class HierarchyPropagator(ModuleBase):
    def __init__(self,
            schemaVersion,
            propagated_info_len,
            node_info_propagator_stack_depth,
            disable_batch_norm,
            input_dropout_p=0,
            dropout_p=0,
            device=None):
        super().__init__(schemaVersion, device)
        self.propagated_info_len = propagated_info_len
        self.node_info_propagator_stack_depth = node_info_propagator_stack_depth
        self.disable_batch_norm = disable_batch_norm

        self.input_dropout = nn.Dropout(p=input_dropout_p)

        # Batch norm on propagated info.
        if not self.disable_batch_norm:
            self.batchNormPropagatedInfo = nn.BatchNorm1d(num_features=propagated_info_len)

        # Parent info gate.
        self.transformParentInfo = nn.Linear(propagated_info_len, propagated_info_len)
        self.overlayParentInfo = torch.nn.GRUCell(propagated_info_len, propagated_info_len)

        # Children info gate.
        self.transformChildrenInfo = nn.Linear(propagated_info_len, propagated_info_len)
        self.overlayChildrenInfo = torch.nn.GRUCell(propagated_info_len, propagated_info_len)

        # GRU output dropout.
        self.dropout = nn.Dropout(p=dropout_p)

    def reset_parameters(self, device):
        if not self.disable_batch_norm:
            self.batchNormPropagatedInfo.reset_parameters()
        self.overlayParentInfo.reset_parameters()
        self.transformParentInfo.reset_parameters()
        self.overlayChildrenInfo.reset_parameters()
        self.transformChildrenInfo.reset_parameters()

    def singleStepSchema(self, schemaVersion):
        """
        Method to step version numbering.
        """
        if schemaVersion is 0:
            pass
        elif schemaVersion is 1:
            self.transformParentInfo = nn.Linear(self.propagated_info_len, self.propagated_info_len)
            self.transformChildrenInfo = nn.Linear(self.propagated_info_len, self.propagated_info_len)
        else:
            super().singleStepSchema(schemaVersion)

    @methodProfiler
    def forward(self,
            propagatedNodeInfoByNdfo,
            parentSelectorByNdfo,
            childSelectorByNdfoList,
            decreasingFanoutsFactor,
            tensorBoardHook,
            dataDebugHook=None,
    ):
        """
        Implements information flow through attributes.
        Inputs:
            propagatedNodeInfoByNdfo
                Information propagated upto each XML node, in the NDFO order.
            parentSelectorByNdfo:
                Each index position ndfo within parentSelectorByNdfo corresponds to
                an XML node. The value at index position parentNdfo links the XML node
                at index position ndfo with its parent node at index position parentNdfo.
            childSelectorByNdfoList
                A list of childSelectorByNdfo
                childSelectorByNdfo:
                    Each index position ndfo within childSelectorByNdfo corresponds to
                    an XML node. The value childNdfo at index position ndfo links the XML
                    node at index position ndfo with its child XML node at index position
                    childNdfo.
            decreasingFanoutsFactor:
                A tensor containing number of children of an XML node.
                Provided in decreasing order of values(i.e. in NDFO order).
                Used as Scaling factors.
        Outputs:
            propagatedNodeInfoByNdfo:
                Information(*also incorporating info from neighbors*) propagated upto each
                XML node, in the NDFO order.
        """
        dataDebugHook(propagatedNodeInfoByNdfo, "ndfo")

        # Apply input dropout. Better to do after resize as input bits are non-uniform.
        propagatedNodeInfoByNdfo = self.input_dropout(propagatedNodeInfoByNdfo)

        # Apply batch norm.
        if not self.disable_batch_norm and propagatedNodeInfoByNdfo.shape[0] > 1:
            propagatedNodeInfoByNdfo = self.batchNormPropagatedInfo(propagatedNodeInfoByNdfo)
        dataDebugHook(propagatedNodeInfoByNdfo, "ndfo")

        decreasingFanoutsFactor = decreasingFanoutsFactor.unsqueeze(-1)
        
        # Run propagation loop.
        for i in range(self.node_info_propagator_stack_depth):
            # Prepare parent info for propagation into new nodeInfoPropagated.
            propagatedParentInfoByNdfo = propagatedNodeInfoByNdfo[parentSelectorByNdfo, ...]

            # Apply linearity on parent info.
            testPropagatedParentInfoByNdfo = propagatedParentInfoByNdfo
            propagatedParentInfoByNdfo = self.transformParentInfo(propagatedParentInfoByNdfo)

            # Compute children info to propagate to each node.
            propagatedChildrenInfoByNdfo = torch.tensor([], device=self.device)
            for childSelectorByNdfo in childSelectorByNdfoList:
                propagatedCurChildInfoByNdfo = propagatedNodeInfoByNdfo[childSelectorByNdfo, ...]

                # Apply linearity on children info.
                propagatedCurChildInfoByNdfo = self.transformChildrenInfo(propagatedCurChildInfoByNdfo)

                if not propagatedChildrenInfoByNdfo.shape[0]:
                    # First iteration of the loop.
                    propagatedChildrenInfoByNdfo = propagatedCurChildInfoByNdfo
                else:
                    assert(propagatedCurChildInfoByNdfo.shape[0] >= propagatedChildrenInfoByNdfo.shape[0])
                    # If the fanout increases in current iteration, pad neighbor infos by the deficit.
                    if propagatedCurChildInfoByNdfo.shape[0] > propagatedChildrenInfoByNdfo.shape[0]:
                        deficit = propagatedCurChildInfoByNdfo.shape[0] - propagatedChildrenInfoByNdfo.shape[0]
                        propagatedChildrenInfoByNdfo = nn.ZeroPad2d((0, 0, 0, deficit))(propagatedChildrenInfoByNdfo)
                    propagatedChildrenInfoByNdfo = propagatedChildrenInfoByNdfo + propagatedCurChildInfoByNdfo

            # Propagate the parent information using GRU cell to obtain updated node info.
            propagatedNodeInfoByNdfo = self.overlayParentInfo(propagatedParentInfoByNdfo, propagatedNodeInfoByNdfo)

            if propagatedChildrenInfoByNdfo.shape[0]:
                # Normalize propagatedChildrenInfoByNdfo by child count.
                propagatedChildrenInfoByNdfo = propagatedChildrenInfoByNdfo / decreasingFanoutsFactor

            # There may still be some rows that do not have children.
            deficitStart = propagatedChildrenInfoByNdfo.shape[0]
            finalDeficit = propagatedNodeInfoByNdfo.shape[0] - deficitStart
            
            if finalDeficit:
                # Keep original info, when no children.                    
                nodeInfoPropgatedByDfoWhenNoChild = propagatedNodeInfoByNdfo[deficitStart:]

            # Propagate children information using GRU cell to obtain updated node info.
            propagatedNodeInfoByNdfo = propagatedNodeInfoByNdfo[0:deficitStart]
            if propagatedChildrenInfoByNdfo.shape[0]:
                propagatedNodeInfoByNdfo = self.overlayChildrenInfo(
                    propagatedChildrenInfoByNdfo,
                    propagatedNodeInfoByNdfo,
                )

            if finalDeficit:
                propagatedNodeInfoByNdfo = torch.cat([
                    # Propagate children information using GRU cell to obtain updated node info.
                    propagatedNodeInfoByNdfo,
                    nodeInfoPropgatedByDfoWhenNoChild,
                ])

            # Apply GRU output dropout.
            propagatedNodeInfoByNdfo = self.dropout(propagatedNodeInfoByNdfo)

        return propagatedNodeInfoByNdfo

if __name__ == "__main__":
    encoded_node_vec_len=1
    propagated_info_len=1
    max_node_count=9
    node_info_propagator_stack_depth=1
    treeIndex2NodeIndex2NbrIndices = [
            [ # Node count = 3
                (0, [2]),
                (2, []),
                (0, [1]),
            ],
            [ # Node count = 7
               (0, [1, 2]),
                (0, []),
                (0, [3]),
                (2, [5]),
                (5, []),
                (3, [4, 6]),
                (5, []),
            ],
            [ # Node count = 9
                (0, [4, 5]),
                (5, []),
                (4, []),
                (5, []),
                (0, [2, 7]),
                (0, [1, 3, 6, 8]),
                (5, []),
                (4, []),
                (5, []),
            ],
            [ # Node count = 4
                (0, [1]),
                (0, [2]),
                (1, [3]),
                (2, []),
            ],
            [ # Node count = 5
                (0, [4]),
                (2, []),
                (3, [1]),
                (4, [2]),
                (0, [3]),
            ],
    ]

    sampleCount = len(treeIndex2NodeIndex2NbrIndices)

    torch.manual_seed(4)
    hierarchyPropagator = HierarchyPropagator(
        encoded_node_vec_len,
        propagated_info_len,
        max_node_count,
        node_info_propagator_stack_depth,
        disable_batch_norm=True,
    )

    nodeInfosTensor = torch.rand(sampleCount, max_node_count, encoded_node_vec_len)

    nodeInfoPropagated2 = hierarchyPropagator.test_forward(
        treeIndex2NodeIndex2NbrIndices,
        nodeInfosTensor,
    )

    nodeInfoPropagated = hierarchyPropagator.forward(
        treeIndex2NodeIndex2NbrIndices,
        nodeInfosTensor,
        None,
    )
