from fastapi import APIRouter

from app.api.v1.module_storage.file.controller import StorageFileRouter
from app.api.v1.module_storage.source.controller import StorageSourceRouter
from app.api.v1.module_storage.transfer.controller import StorageTransferRouter

storage_router = APIRouter(prefix="/storage")

storage_router.include_router(StorageSourceRouter)
storage_router.include_router(StorageFileRouter)
storage_router.include_router(StorageTransferRouter)
