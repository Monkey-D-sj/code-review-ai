"""存储源核心能力聚合导出。

把底层加解密实现以稳定的公共别名对外暴露，供其他模块复用，
业务方不直接依赖 encrypt 模块的内部符号。
"""

from .encrypt import decrypt_password as decrypt_storage_password
