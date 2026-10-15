"""Socket names under the runtime dir (paths.host_sock)."""

import os

from agentail import paths


def test_host_sock_names(runtime_env):
    plain = paths.host_sock("gpu7")
    assert plain.name == "gpu7.sock"
    # aliases that would map to the same name get different sockets
    a, b = paths.host_sock("me@gpu7"), paths.host_sock("me_gpu7")
    assert a != b and b.name == "me_gpu7.sock" and a.name.startswith("me_gpu7-")
    # an alias too long for a Unix socket path still gets one that fits
    long = paths.host_sock("a" * 128)
    assert len(os.fsencode(long)) <= paths.MAX_SOCK_PATH
    assert long == paths.host_sock("a" * 128)  # stable
