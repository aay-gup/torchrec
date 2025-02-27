#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import itertools
import multiprocessing
import os
import unittest
from typing import Callable

import torch
import torch.distributed as dist
import torchrec.distributed.comm_ops as comm_ops
from torchrec.distributed.utils import none_throws
from torchrec.test_utils import get_free_port, seed_and_log


class TestAllToAll(unittest.TestCase):
    @seed_and_log
    def setUp(self) -> None:
        os.environ["MASTER_ADDR"] = str("localhost")
        os.environ["MASTER_PORT"] = str(get_free_port())
        os.environ["GLOO_DEVICE_TRANSPORT"] = "TCP"
        os.environ["NCCL_SOCKET_IFNAME"] = "lo"
        self.WORLD_SIZE = 2

    def tearDown(self) -> None:
        del os.environ["GLOO_DEVICE_TRANSPORT"]
        del os.environ["NCCL_SOCKET_IFNAME"]
        super().tearDown()

    def _run_multi_process_test(
        self,
        world_size: int,
        backend: str,
        callable: Callable[[], None],
    ) -> None:
        processes = []
        ctx = multiprocessing.get_context("spawn")
        for rank in range(world_size):
            p = ctx.Process(
                target=callable,
                args=(
                    rank,
                    world_size,
                    backend,
                ),
            )
            p.start()
            processes.append(p)

        for p in processes:
            p.join()
            self.assertEqual(0, p.exitcode)

    @classmethod
    def _test_alltoallv(
        cls,
        rank: int,
        world_size: int,
        backend: str,
    ) -> None:
        dist.init_process_group(rank=rank, world_size=world_size, backend=backend)
        device = torch.device(f"cuda:{rank}")

        torch.cuda.set_device(device)

        B_global = 10
        D0 = 8
        D1 = 9

        input_embedding0 = torch.rand(
            (B_global, D0),
            device=device,
            requires_grad=True,
        )
        input_embedding1 = torch.rand(
            (B_global, D1),
            device=device,
            requires_grad=True,
        )

        input_embeddings = [input_embedding0, input_embedding1]
        out_split = [17, 17]

        a2a_req = comm_ops.alltoallv(input_embeddings, out_split)
        v_embs_out = a2a_req.wait()
        res = torch.cat(v_embs_out, dim=1).cpu()
        assert tuple(res.size()) == (5, 34)
        dist.destroy_process_group()

    # pyre-fixme[56]: Pyre was not able to infer the type of argument
    #  `torch.cuda.device_count() = 0` to decorator factory `unittest.skipIf`.
    @unittest.skipIf(
        torch.cuda.device_count() < 2, "Need at least two ranks to run this test"
    )
    def test_alltoallv(self) -> None:
        self._run_multi_process_test(
            world_size=self.WORLD_SIZE,
            backend="nccl",
            # pyre-ignore [6]
            callable=self._test_alltoallv,
        )

    @classmethod
    def _test_alltoall_sequence(
        cls,
        rank: int,
        world_size: int,
        backend: str,
    ) -> None:
        dist.init_process_group(rank=rank, world_size=world_size, backend=backend)
        device = torch.device(f"cuda:{rank}")
        torch.cuda.set_device(device)

        ranks = 2
        tables_mp = [[0], [1, 2]]
        lengths_dp = [
            torch.tensor([[1, 2], [1, 1], [2, 1]], dtype=torch.int),
            torch.tensor([[1, 2], [2, 1], [3, 1]], dtype=torch.int),
        ]  # W, T_g, B_l
        lengths_a2a = [
            torch.tensor([[[1, 2]], [[1, 2]]], dtype=torch.int),  # Rank 0
            torch.tensor(
                [
                    [[1, 1], [2, 1]],  # from Rank 0
                    [[2, 1], [3, 1]],  # from rank 1
                ],
                dtype=torch.int,
            ),  # Rank 1
        ]  # W, W, T_l, B_l
        lengths_mp = [
            torch.tensor(
                [
                    [1, 2, 1, 2],
                ],
                dtype=torch.int,
            ),
            torch.tensor([[1, 1, 2, 1], [2, 1, 3, 1]], dtype=torch.int),
        ]  # w, t_l, b_g
        input_seg = list(itertools.accumulate([0] + [len(i) for i in tables_mp]))
        input_splits = [
            [
                int(lengths_dp[i][input_seg[j] : input_seg[j + 1], :].sum())
                for j in range(ranks)
            ]
            for i in range(ranks)
        ]
        output_splits = [lengths_a2a[i].sum(dim=(1, 2)).tolist() for i in range(ranks)]
        table_dim = 3
        num_features_per_rank = [len(features) for features in tables_mp]
        seq_all2all_forward_recat = []
        for j in range(ranks):
            for i in range(num_features_per_rank[rank]):
                seq_all2all_forward_recat.append(j + i * ranks)
        seq_all2all_forward_recat_tensor = torch.IntTensor(seq_all2all_forward_recat)
        seq_all2all_backward_recat = []
        for i in range(num_features_per_rank[rank]):
            for j in range(ranks):
                seq_all2all_backward_recat.append(i + j * num_features_per_rank[rank])

        seq_all2all_backward_recat_tensor = torch.IntTensor(seq_all2all_backward_recat)
        input_embeddings = torch.rand(
            int(lengths_mp[rank].sum()),
            table_dim,
            device=device,
            requires_grad=True,
        )
        lengths_after_sparse_data_all2all = torch.IntTensor(lengths_mp[rank])
        a2a_req = comm_ops.alltoall_sequence(
            a2a_sequence_embs_tensor=input_embeddings.cuda(),
            forward_recat_tensor=seq_all2all_forward_recat_tensor.cuda(),
            backward_recat_tensor=seq_all2all_backward_recat_tensor.cuda(),
            lengths_after_sparse_data_all2all=lengths_after_sparse_data_all2all.cuda(),
            input_splits=input_splits[rank],
            output_splits=output_splits[rank],
        )
        seq_embs_out = a2a_req.wait()
        seq_embs_out.backward(seq_embs_out)
        grad = input_embeddings.grad
        assert torch.equal(
            input_embeddings.cpu().detach(),
            # pyre-fixme[16]: Optional type has no attribute `cpu`.
            grad.cpu().detach() * world_size,
        )

        dist.destroy_process_group()

    # pyre-fixme[56]: Pyre was not able to infer the type of argument
    #  `torch.cuda.device_count() = 0` to decorator factory `unittest.skipIf`.
    @unittest.skipIf(
        torch.cuda.device_count() < 2, "Need at least two ranks to run this test"
    )
    def test_alltoall_sequence(self) -> None:
        self._run_multi_process_test(
            world_size=self.WORLD_SIZE,
            backend="nccl",
            # pyre-ignore [6]
            callable=self._test_alltoall_sequence,
        )

    @classmethod
    def _test_alltoall_pooled(
        cls,
        rank: int,
        world_size: int,
        backend: str,
    ) -> None:
        dist.init_process_group(rank=rank, world_size=world_size, backend=backend)
        device = torch.device(f"cuda:{rank}")
        torch.cuda.set_device(device)

        # Each rank's local batch size
        batch_size_per_rank = [4, 4]
        # Global batch size is the sum of all rank's local batch size
        B_global = sum(batch_size_per_rank)
        # sum of dimensions of the embedding tables hosted on each rank
        dim_sum_per_rank = [8, 8]
        D_local_sum = dim_sum_per_rank[rank]

        # Construct pooled embeddings
        pooled_embeddings = torch.randn([B_global, D_local_sum], requires_grad=True).to(
            device
        )
        pooled_embeddings.retain_grad()

        # Save a copy for running again with gradient division
        pooled_embeddings_gradient_division = (
            pooled_embeddings.detach().clone().to(device)
        )
        pooled_embeddings_gradient_division.requires_grad = True
        pooled_embeddings_gradient_division.retain_grad()

        # Run alltoall_pooled with gradient division disabled
        comm_ops.set_gradient_division(False)
        a2a_embedding = comm_ops.alltoall_pooled(
            pooled_embeddings, batch_size_per_rank, dim_sum_per_rank
        ).wait()
        a2a_embedding.retain_grad()
        a2a_embedding.backward(a2a_embedding)

        # Run alltoall_pooled with gradient division enabled
        comm_ops.set_gradient_division(True)
        a2a_embedding_gradient_division = comm_ops.alltoall_pooled(
            pooled_embeddings_gradient_division, batch_size_per_rank, dim_sum_per_rank
        ).wait()
        a2a_embedding_gradient_division.retain_grad()
        a2a_embedding_gradient_division.backward(a2a_embedding_gradient_division)

        assert torch.equal(
            none_throws(pooled_embeddings.grad),
            torch.mul(
                none_throws(pooled_embeddings_gradient_division.grad), world_size
            ),
        )
        dist.destroy_process_group()

    # pyre-fixme[56]: Pyre was not able to infer the type of argument
    #  `torch.cuda.device_count() = 0` to decorator factory `unittest.skipIf`.
    @unittest.skipIf(
        torch.cuda.device_count() < 2, "Need at least two ranks to run this test"
    )
    def test_alltoall_pooled(self) -> None:
        self._run_multi_process_test(
            world_size=self.WORLD_SIZE,
            backend="nccl",
            # pyre-ignore [6]
            callable=self._test_alltoall_pooled,
        )
