import os
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends, File, Form, Query, Security, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.module_storage.core.base import StorageObject
from app.common.response import ResponseSchema, SuccessResponse, UploadFileResponse
from app.core.base_schema import AuthSchema
from app.core.dependencies import AuthPermission, db_getter
from app.core.router_class import OperationLogRoute

from .service import StorageFileService

StorageFileRouter = APIRouter(route_class=OperationLogRoute, prefix="/file", tags=["存储文件"])


def _delete_temp_file(path: str) -> None:
    """响应发送后清理临时下载文件。"""
    try:
        os.unlink(path)
    except OSError:
        pass


@StorageFileRouter.post("/upload", summary="上传文件到存储源", response_model=ResponseSchema[dict])
async def upload_storage_file_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_storage:file:upload"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    file: Annotated[UploadFile, File(description="上传文件")],
    source_id: Annotated[int | None, Form(description="存储源ID（不传使用默认存储源）")] = None,
    remote_path: Annotated[str | None, Form(description="远端目录路径（不传自动生成文件名）")] = None,
) -> JSONResponse:
    result = await StorageFileService(auth, db).upload(source_id=source_id, file=file, remote_path=remote_path)
    return SuccessResponse(data=result, msg="上传文件成功")


@StorageFileRouter.post("/download", summary="下载存储源文件", response_model=None)
async def download_storage_file_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_storage:file:download"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    background_tasks: BackgroundTasks,
    remote_path: Annotated[str, Body(description="远端文件路径")],
    source_id: Annotated[int | None, Body(description="存储源ID（不传使用默认存储源）")] = None,
) -> UploadFileResponse:
    local_path, file_name = await StorageFileService(auth, db).download(source_id=source_id, remote_path=remote_path)
    background_tasks.add_task(_delete_temp_file, local_path)
    return UploadFileResponse(file_path=local_path, filename=file_name)


@StorageFileRouter.delete("/delete", summary="删除存储源文件", response_model=ResponseSchema[None])
async def delete_storage_file_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_storage:file:delete"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    remote_path: Annotated[str, Body(description="远端文件路径")],
    source_id: Annotated[int | None, Body(description="存储源ID（不传使用默认存储源）")] = None,
) -> JSONResponse:
    await StorageFileService(auth, db).delete(source_id=source_id, remote_path=remote_path)
    return SuccessResponse(msg="删除文件成功")


@StorageFileRouter.get("/list", summary="查询存储源文件列表", response_model=ResponseSchema[list[StorageObject]])
async def list_storage_file_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_storage:file:query"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    source_id: Annotated[int | None, Query(description="存储源ID（不传使用默认存储源）")] = None,
    prefix: Annotated[str | None, Query(description="目录前缀（可选）")] = None,
) -> JSONResponse:
    result = await StorageFileService(auth, db).list(source_id=source_id, prefix=prefix or "")
    return SuccessResponse(data=result, msg="查询文件列表成功")


@StorageFileRouter.get("/url", summary="获取文件访问URL", response_model=ResponseSchema[str | None])
async def get_storage_file_url_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_storage:file:query"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    remote_path: Annotated[str, Query(description="远端文件路径")],
    source_id: Annotated[int | None, Query(description="存储源ID（不传使用默认存储源）")] = None,
    expire: Annotated[int, Query(description="有效期（秒）", ge=60, le=86400)] = 3600,
) -> JSONResponse:
    result = await StorageFileService(auth, db).get_url(source_id=source_id, remote_path=remote_path, expire=expire)
    return SuccessResponse(data=result, msg="获取文件URL成功")


@StorageFileRouter.post("/copy", summary="复制/移动文件", response_model=ResponseSchema[dict])
async def copy_or_move_storage_file_controller(
    auth: Annotated[AuthSchema, Security(AuthPermission(["module_storage:file:update"]))],
    db: Annotated[AsyncSession, Depends(db_getter)],
    source_id: Annotated[int | None, Body(description="源存储源ID（不传使用默认存储源）")] = None,
    source_path: Annotated[str, Body(description="源文件路径")] = "",
    target_id: Annotated[int, Body(description="目标存储源ID")] = 0,
    target_path: Annotated[str, Body(description="目标路径")] = "",
    move: Annotated[bool, Body(description="是否为移动（true 移动/重命名，false 复制）")] = False,
) -> JSONResponse:
    result = await StorageFileService(auth, db).copy_or_move(
        source_id=source_id,
        source_path=source_path,
        target_id=target_id,
        target_path=target_path,
        move=move,
    )
    return SuccessResponse(data=result, msg="操作文件成功")
