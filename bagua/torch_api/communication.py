import logging
import multiprocessing
import torch
import torch.distributed as dist
import torch.distributed.distributed_c10d as c10d
import bagua_core as B
from flask import Flask
from bagua.service import AutotuneService
from .env import (
    get_world_size,
    get_rank,
    get_local_rank,
    get_local_size,
    get_master_addr,
    get_bagua_service_port,
    get_default_bucket_size,
    get_bagua_service_port,
)
from ..service.autotune_service import AutotuneClient
from .exceptions import RepeatedInitializationError
from .utils import flatten, unflatten, to_bagua_datatype
from ..bagua_define import BaguaHyperparameter

_global_state = None
_autotune_server = None


def _get_global_state():
    global _global_state
    return _global_state


def is_initialized():
    """
    Checking if bagua global communication state has been initialized
    """
    global _global_state
    return _global_state is not None


def init_process_group():
    """Initializes the PyTorch builtin distributed process group, and this will
    also initialize the distributed package, should be executed before all the
    APIs of bagua.

    Raises:
        RepeatedInitializationError: If you run this function repeatedly

    Examples::
        >>> import bagua.torch_api as bagua
        >>> bagua.init_process_group()
        >>> model = torch.nn.Sequential(
        ...    torch.nn.Linear(D_in, H),
        ...    torch.nn.ReLU(),
        ...    torch.nn.Linear(H, D_out),
        ...    )
        >>> optimizer = torch.optim.SGD(
        ...    model.parameters(),
        ...    lr=0.01,
        ...    momentum=0.9
        ...    )
        >>> model, optimizer = bagua_init(model, optimizer)
    """
    if is_initialized():
        raise RepeatedInitializationError()

    if not dist.is_initialized():
        torch.distributed.init_process_group(
            backend="nccl", init_method="env://"
        )  # fmt: off

    store = c10d._get_default_store()

    if get_rank() == 0:
        global _autotune_server

        autotune_service = AutotuneService(
            world_size=get_world_size(),
            default_bucket_size=get_default_bucket_size(),
        )
        app = Flask(__name__)
        app = autotune_service.setup_app(app)
        _autotune_server = multiprocessing.Process(
            target=app.run,
            kwargs={
                "host": "0.0.0.0",
                "port": get_bagua_service_port(),
                "debug": False,
            },
        )
        _autotune_server.daemon = True
        _autotune_server.start()

    global _global_state
    _global_state = BaguaGlobalState(store)


class BaguaGlobalState(object):
    def __init__(self, store=None, device_id=None):
        if device_id is None:
            device_id = get_local_rank()
        self.backend = B.BaguaCommBackendPy(100, device_id=device_id)
        self.stream = torch.cuda.Stream(priority=-1)
        self.store = store
        self.hyperparameters = BaguaHyperparameter()
        self.hyperparameters_service_client = AutotuneClient(
            get_master_addr(), get_bagua_service_port()
        )
        self.internode_communicator = init_bagua_inter_communicator(
            stream=self.stream, leader_rank=0, store=self.store, device_id=device_id
        )
        self.intranode_communicator = init_bagua_intra_communicator(
            stream=self.stream, store=self.store, device_id=device_id
        )
        self.global_communicator = init_bagua_communicator(
            stream=self.stream, store=self.store, device_id=device_id
        )

    def get_internode_communicator(self):
        return self.internode_communicator

    def get_intranode_communicator(self):
        return self.intranode_communicator

    def get_global_communicator(self):
        return self.global_communicator

    def get_backend(self):
        return self.backend


def get_bagua_hyperparameters():
    return _global_state.hyperparameters


def get_hyperparameters_service_client():
    return _global_state.hyperparameters_service_client


def gen_nccl_unique_id(comm_type: str, root=0, store=None):
    key = f"{comm_type}-{root}-unique_id"

    if store is None:
        store = c10d._get_default_store()

    if get_rank() == root:
        idstr = B.BaguaSingleCommunicatorPy.generate_nccl_unique_id_str()
        store.set(key, idstr)
    else:
        idstr = store.get(key)
        idstr = str(idstr, encoding="utf-8")

    return idstr


def init_bagua_inter_communicator(stream, leader_rank=0, store=None, device_id=None):
    if device_id is None:
        device_id = get_local_rank()
    nccl_unique_id = gen_nccl_unique_id(
        "bagua_inter_comm", root=leader_rank, store=store
    )

    if get_rank() % get_local_size() != leader_rank:
        return None

    comm = B.BaguaSingleCommunicatorPy(
        rank=get_rank() // get_local_size(),
        nranks=get_world_size() // get_local_size(),
        device_id=device_id,
        stream_ptr=stream.cuda_stream,
        nccl_unique_id_str=nccl_unique_id,
    )
    comm.cuda_stream = stream
    logging.debug(
        f"init bagua internode communicator ok, global rank: {dist.get_rank()} rank: {comm.rank()}"
    )
    return comm


def init_bagua_intra_communicator(stream, store=None, device_id=None):
    if device_id is None:
        device_id = get_local_rank()
    nccl_unique_id = gen_nccl_unique_id(
        "bagua_intra_comm",
        root=get_rank() // get_local_size() * get_local_size(),
        store=store,
    )

    comm = B.BaguaSingleCommunicatorPy(
        rank=get_rank() % get_local_size(),
        nranks=get_local_size(),
        device_id=device_id,
        stream_ptr=stream.cuda_stream,
        nccl_unique_id_str=nccl_unique_id,
    )
    comm.cuda_stream = stream
    logging.debug(
        f"init bagua intranode communicator ok, global rank: {dist.get_rank()} rank: {comm.rank()}"
    )
    return comm


def init_bagua_communicator(stream, store=None, device_id=None):
    if device_id is None:
        device_id = get_local_rank()
    nccl_unique_id = gen_nccl_unique_id("bagua_global_comm", store=store)

    comm = B.BaguaSingleCommunicatorPy(
        rank=get_rank(),
        nranks=get_world_size(),
        device_id=device_id,
        stream_ptr=stream.cuda_stream,
        nccl_unique_id_str=nccl_unique_id,
    )
    comm.cuda_stream = stream
    logging.debug(
        f"init bagua global communicator ok, global rank: {dist.get_rank()} rank: {comm.rank()}"
    )
    return comm


def broadcast_coalesced(tensors, root=0, comm: B.BaguaSingleCommunicatorPy = None):
    for tensor in tensors:
        assert tensor.device != torch.device(
            "cpu"
        ), "input tensors must be CUDA and dense"

    if comm is None:
        comm = _get_global_state().get_global_communicator()

    event = torch.cuda.current_stream().record_event()
    comm.cuda_stream.wait_event(event)

    with torch.cuda.stream(comm.cuda_stream):
        coalesced = flatten(tensors)
        b_coalesced = B.BaguaTensorPy(
            ptr=coalesced.data_ptr(),
            num_elem=coalesced.numel(),
            num_elem_allocated=coalesced.numel(),
            dtype=to_bagua_datatype(coalesced.dtype),
            device_id=coalesced.device.index,
        )
        comm.broadcast(b_coalesced, root)

        for buf, synced in zip(tensors, unflatten(coalesced, tensors)):
            buf.copy_(synced)

    torch.cuda.synchronize()


def broadcast(tensor, root=0, comm: B.BaguaSingleCommunicatorPy = None):
    r"""Broadcasts the tensor to the whole communicator.

    `tensor` must have the same number of elements in all processes
    participating in the collective.

    Args:
        tensor (torch.Tensor): Data to be sent if `root` is the rank of
            current process, and tensor to be used to save received data
            otherwise.
        root (int, optional): Source rank. Defaults to 0.
        comm (B.BaguaSingleCommunicatorPy, optional): The bagua communicator
            to work on. If None, the global bagua communicator will be used.
            Defaults to None.
    """  # noqa: W293

    assert tensor.device != torch.device("cpu"), "input tensor must be CUDA and dense"

    if comm is None:
        comm = _get_global_state().get_global_communicator()

    event = torch.cuda.current_stream().record_event()
    comm.cuda_stream.wait_event(event)

    with torch.cuda.stream(comm.cuda_stream):
        b_tensor = B.BaguaTensorPy(
            ptr=tensor.data_ptr(),
            num_elem=tensor.numel(),
            num_elem_allocated=tensor.numel(),
            dtype=to_bagua_datatype(tensor.dtype),
            device_id=tensor.device.index,
        )
        comm.broadcast(b_tensor, root)

    torch.cuda.synchronize()


def allreduce_coalesced(
    tensors,
    comm: B.BaguaSingleCommunicatorPy = None,
    average: bool = True,
):
    for tensor in tensors:
        assert tensor.device != torch.device(
            "cpu"
        ), "input tensors must be CUDA and dense"

    if comm is None:
        comm = _get_global_state().get_global_communicator()

    event = torch.cuda.current_stream().record_event()
    comm.cuda_stream.wait_event(event)

    with torch.cuda.stream(comm.cuda_stream):
        coalesced = flatten(tensors)
        b_coalesced = B.BaguaTensorPy(
            ptr=coalesced.data_ptr(),
            num_elem=coalesced.numel(),
            num_elem_allocated=coalesced.numel(),
            dtype=to_bagua_datatype(coalesced.dtype),
            device_id=coalesced.device.index,
        )
        comm.allreduce(b_coalesced)

        if average:
            coalesced /= comm.nranks()

        for buf, synced in zip(tensors, unflatten(coalesced, tensors)):
            buf.copy_(synced)

    torch.cuda.synchronize()


def allreduce(
    tensor,
    average: bool = True,
    comm: B.BaguaSingleCommunicatorPy = None,
):
    """Reduces the tensor data across all machines in such a way that all get
    the final result. After the call tensor is going to be bitwise identical
    in all processes.

    Args:
        tensor (torch.Tensor): Input and output of the collective. The
            function operates in-place.
        average (bool, optional): Average the reduced tensor or
            not, Defaults to True.
        comm (B.BaguaSingleCommunicatorPy, optional): The bagua communicator to
            work on. If None the global bagua communicator will be used.
            Defaults to None.

    Examples:
        >>> from bagua.torch_api import allreduce
        >>> # All tensors below are of torch.int64 type.
        >>> # We have 2 process groups, 2 ranks.
        >>> tensor = torch.arange(2, dtype=torch.int64) + 1 + 2 * rank
        >>> tensor
        tensor([1, 2]) # Rank 0
        tensor([3, 4]) # Rank 1
        >>> allreduce(tensor)
        >>> tensor
        tensor([4, 6]) # Rank 0
        tensor([4, 6]) # Rank 1

        >>> # All tensors below are of torch.cfloat type.
        >>> # We have 2 process groups, 2 ranks.
        >>> tensor = torch.tensor([1+1j, 2+2j], dtype=torch.cfloat) + 2 * rank * (1+1j)
        >>> tensor
        tensor([1.+1.j, 2.+2.j]) # Rank 0
        tensor([3.+3.j, 4.+4.j]) # Rank 1
        >>> allreduce(tensor)
        >>> tensor
        tensor([4.+4.j, 6.+6.j]) # Rank 0
        tensor([4.+4.j, 6.+6.j]) # Rank 1
    """  # noqa: E501

    assert tensor.device != torch.device("cpu"), "input tensor must be CUDA and dense"

    if comm is None:
        comm = _get_global_state().get_global_communicator()

    event = torch.cuda.current_stream().record_event()
    comm.cuda_stream.wait_event(event)

    with torch.cuda.stream(comm.cuda_stream):
        b_tensor = B.BaguaTensorPy(
            ptr=tensor.data_ptr(),
            num_elem=tensor.numel(),
            num_elem_allocated=tensor.numel(),
            dtype=to_bagua_datatype(tensor.dtype),
            device_id=tensor.device.index,
        )
        comm.allreduce(b_tensor)

        if average:
            tensor /= comm.nranks()

    torch.cuda.synchronize()
