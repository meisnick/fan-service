#!/usr/bin/env python3
"""Read LSI/Broadcom SAS HBA chip temperatures via the mpt3sas driver ioctl (MPI IO Unit Page 7).
Read-only: sends MPI2 CONFIG requests (PAGE_HEADER, then READ_CURRENT). Needs root and /dev/mpt3ctl.
Covers SAS2 (2008/2116...) and SAS3 (3008/3108...) controllers driven by mpt3sas."""
import ctypes, fcntl, glob, os, struct

MPT3COMMAND = (3 << 30) | (72 << 16) | (ord("L") << 8) | 20   # _IOWR('L', 20, struct mpt3_ioctl_command) (72 bytes)
HDR = 68                       # offset of mf[] in struct mpt3_ioctl_command (x86_64)
SGE_OFF = 28                   # Mpi2ConfigRequest_t: PageBufferSGE offset


def _cfg(fd, ioc, action, page_type, page_num, ver=0, length=0):
    req = bytearray(SGE_OFF)
    req[0] = action                      # 0 = PAGE_HEADER, 1 = READ_CURRENT
    req[3] = 0x04                        # MPI2_FUNCTION_CONFIG
    req[20], req[21], req[22], req[23] = ver, length, page_num, page_type
    data = ctypes.create_string_buffer(max(4, length * 4))
    reply = ctypes.create_string_buffer(128)
    buf = ctypes.create_string_buffer(HDR + len(req) + 8)
    struct.pack_into("<IIII QQQQ IIIII", buf, 0,
                     ioc, 0, 0, 10,
                     ctypes.addressof(reply), ctypes.addressof(data) if length else 0, 0, 0,
                     128, length * 4, 0, 0, SGE_OFF // 4)
    buf[HDR:HDR + len(req)] = bytes(req)
    fcntl.ioctl(fd, MPT3COMMAND, buf)
    r = reply.raw
    ioc_status = struct.unpack_from("<H", r, 14)[0] & 0x7FFF
    return ioc_status, r[20], r[21], data.raw[:length * 4]


def read_temps():
    """-> [{host, ioc, board, fw, pci, ioc_temp_c, board_temp_c}]"""
    # MPI 2.0 controllers (SAS2008/2116...) are numbered separately and answer on /dev/mpt2ctl,
    # MPI 2.5+ (SAS3008...) on /dev/mpt3ctl. version_mpi in sysfs tells which.
    fds = {k: os.open(p, os.O_RDWR) for k, p in (("2", "/dev/mpt2ctl"), ("3", "/dev/mpt3ctl")) if os.path.exists(p)}
    out = []
    try:
        for h in sorted(glob.glob("/sys/class/scsi_host/host*")):
            try:
                if open(h + "/proc_name").read().strip() not in ("mpt3sas", "mpt2sas"):
                    continue
                ioc = int(open(h + "/unique_id").read())
                rd = lambda f: open(h + "/" + f).read().strip() if os.path.exists(h + "/" + f) else None
                e = {"host": os.path.basename(h), "ioc": ioc, "board": rd("board_name"), "fw": rd("version_fw"),
                     "pci": os.path.basename(os.path.realpath(h + "/device/..")), "ioc_temp_c": None, "board_temp_c": None}
                mpi = rd("version_mpi") or ""
                fd = fds["2"] if mpi.startswith("200") and "2" in fds else fds["3"]   # e.g. "200.23" = MPI 2.0, "205.32" = MPI 2.5
                e["mpi"] = mpi
                st, ver, ln, _ = _cfg(fd, ioc, 0, 0x00, 7)
                if st == 0 and ln:
                    st, _, _, d = _cfg(fd, ioc, 1, 0x00, 7, ver, ln)
                    if st == 0 and len(d) >= 23:
                        def conv(v, u):
                            return None if u == 0 or v == 0 else (v if u == 2 else round((v - 32) * 5 / 9, 1))
                        e["ioc_temp_c"] = conv(struct.unpack_from("<H", d, 16)[0], d[18])
                        e["board_temp_c"] = conv(struct.unpack_from("<H", d, 20)[0], d[22])
                e["status"] = st
                out.append(e)
            except OSError as ex:
                out.append({"host": os.path.basename(h), "error": str(ex)})
    finally:
        for fd in fds.values():
            os.close(fd)
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(read_temps(), indent=1))
