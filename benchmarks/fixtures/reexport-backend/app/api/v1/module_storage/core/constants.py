from enum import Enum


class StorageProtocol(str, Enum):
    """存储协议枚举"""

    FTP = "ftp"
    FTPS = "ftps"
    SFTP = "sftp"
    S3 = "s3"
    OBS = "obs"
    OSS = "oss"
    COS = "cos"
    LOCAL = "local"


# 各协议默认端口
DEFAULT_PORTS: dict[StorageProtocol, int] = {
    StorageProtocol.FTP: 21,
    StorageProtocol.FTPS: 990,
    StorageProtocol.SFTP: 22,
    StorageProtocol.S3: 443,
    StorageProtocol.OBS: 443,
    StorageProtocol.OSS: 443,
    StorageProtocol.COS: 443,
    StorageProtocol.LOCAL: 0,
}
