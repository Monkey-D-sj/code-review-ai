"""
认证控制器 — TODO: 限流粒度细化
---------------------------------
当前登录（/login）和 OAuth 端点（/oauth/*）共享应用的通用限流配置，
缺少独立的、更严格的限流策略。建议为以下端点配置独立的 RateLimiter：

1. /auth/login — 密码登录
   - 建议: 按 IP + 用户名组合限流，如 5次/分钟/IP + 10次/15分钟/用户
   - 原因: 暴力破解防护

2. /auth/oauth/* — 第三方 OAuth 登录/回调
   - 建议: 按 IP 限流，如 10次/分钟/IP
   - 原因: OAuth 流程可能触发多次重定向，频率稍高于登录

3. /auth/captcha/* — 验证码获取/校验
   - 建议: 按 IP 限流，如 3次/分钟/IP
   - 原因: 防止验证码遍历
"""

import json
import secrets
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Depends, Path, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from redis.asyncio.client import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.module_system.user.crud import UserCRUD
from app.api.v1.module_system.user.schema import UserCreateSchema
from app.api.v1.module_system.user.service import UserService
from app.common.response import ErrorResponse, RedirectContentResponse, ResponseSchema, SuccessResponse
from app.config.setting import settings
from app.core.base_schema import AuthSchema, JWTOutSchema
from app.core.dependencies import db_getter, get_current_user, redis_getter
from app.core.exceptions import CustomException
from app.core.logger import logger
from app.core.redis_crud import RedisCURD
from app.core.router_class import OperationLogRoute
from app.core.security import CustomOAuth2PasswordRequestForm

from .oauth_service import (
    STATE_PREFIX,
    OAuthProvider,
    _callback_url,
    build_authorize_url,
    complete_oauth_login,
    oauth_service_error_redirect,
    oauth_service_frontend_redirect_from_token,
    save_oauth_state,
)
from .schema import (
    CaptchaOutSchema,
    LoginOutSchema,
    SliderCompleteOutSchema,
    SliderCompleteSchema,
    WxLoginSchema,
    WxPhoneLoginSchema,
    WxQrCodeOutSchema,
    WxQrCodeSchema,
)
from .service import (
    CaptchaService,
    LoginService,
)
from .wx_mini_service import (
    code2session,
    ensure_wx_user,
    get_phone_number,
    get_qrcode,
)

AuthRouter = APIRouter(route_class=OperationLogRoute, prefix="/auth", tags=["认证授权"])


@AuthRouter.post("/login", summary="登录", response_model=LoginOutSchema)
async def login_for_access_token_controller(
    request: Request,
    background_tasks: BackgroundTasks,
    redis: Annotated[Redis, Depends(redis_getter)],
    db: Annotated[AsyncSession, Depends(db_getter)],
    login_form: Annotated[CustomOAuth2PasswordRequestForm, Depends()],
) -> JSONResponse | LoginOutSchema:
    login_result = await LoginService.authenticate_user(request=request, redis=redis, login_form=login_form, db=db, background_tasks=background_tasks)

    logger.info(f"用户{login_form.username}登录成功")

    if settings.DOCS_URL in request.headers.get("referer", ""):
        return login_result
    return SuccessResponse(data=login_result, msg="登录成功")


@AuthRouter.post("/token/refresh", summary="刷新token", response_model=ResponseSchema[JWTOutSchema])
async def get_new_token_controller(
    db: Annotated[AsyncSession, Depends(db_getter)],
    redis: Annotated[Redis, Depends(redis_getter)],
    payload: Annotated[str, Body(description="刷新token参数")],
) -> JSONResponse:
    new_token = await LoginService.refresh_token(db=db, redis=redis, refresh_token=payload)
    return SuccessResponse(data=new_token, msg="刷新成功")


@AuthRouter.get("/captcha/get", summary="获取验证码", response_model=ResponseSchema[CaptchaOutSchema])
async def get_captcha_for_login_controller(
    redis: Annotated[Redis, Depends(redis_getter)],
) -> JSONResponse:
    captcha = await CaptchaService.get_captcha(redis=redis)
    return SuccessResponse(data=captcha, msg="获取验证码成功")


@AuthRouter.post("/captcha/slider/complete", summary="滑块验证完成", response_model=ResponseSchema[SliderCompleteOutSchema])
async def slider_complete_controller(
    redis: Annotated[Redis, Depends(redis_getter)],
    body: SliderCompleteSchema,
) -> JSONResponse:
    result = await CaptchaService.slider_complete(redis=redis, captcha_key=body.captcha_key)
    return SuccessResponse(data=result, msg="滑块验证成功")


@AuthRouter.post("/logout", summary="退出登录", response_model=ResponseSchema[None], dependencies=[Depends(get_current_user)])
async def logout_controller(
    redis: Annotated[Redis, Depends(redis_getter)],
    payload: Annotated[str, Body(description="退出登录参数")],
) -> JSONResponse:
    if await LoginService.logout(redis=redis, token=payload):
        logger.info("退出成功")
        return SuccessResponse(msg="退出成功")
    return ErrorResponse(msg="退出失败")


@AuthRouter.get("/oauth/{provider}/login", summary="第三方OAuth跳转")
async def oauth_login_redirect_controller(
    request: Request,
    redis: Annotated[Redis, Depends(redis_getter)],
    provider: Annotated[OAuthProvider, Path(description="wechat | qq | github | gitee")],
    redirect_uri: Annotated[str | None, Query(description="OAuth 完成后浏览器回到的前端登录页完整 URL")] = None,
) -> RedirectResponse:
    allowed = {"wechat", "qq", "github", "gitee"}
    fe = redirect_uri or settings.OAUTH_FRONTEND_FALLBACK
    if provider not in allowed:
        return RedirectContentResponse(
            url=oauth_service_error_redirect(fe, "不支持的 OAuth 渠道"),
            status_code=302,
        )
    if not redirect_uri:
        return RedirectContentResponse(
            url=oauth_service_error_redirect(fe, "缺少 redirect_uri 参数"),
            status_code=302,
        )
    try:
        state = secrets.token_urlsafe(32)
        await save_oauth_state(
            redis=redis,
            state=state,
            provider=provider,
            frontend_redirect=redirect_uri,
        )
        cb = _callback_url(request, provider)
        url = build_authorize_url(provider=provider, callback_url=cb, state=state)
        return RedirectContentResponse(url=url, status_code=302)
    except CustomException as e:
        return RedirectContentResponse(
            url=oauth_service_error_redirect(redirect_uri, e.msg),
            status_code=302,
        )


@AuthRouter.get("/oauth/{provider}/callback", summary="第三方OAuth回调", include_in_schema=False)
async def oauth_callback_controller(
    request: Request,
    background_tasks: BackgroundTasks,
    redis: Annotated[Redis, Depends(redis_getter)],
    db: Annotated[AsyncSession, Depends(db_getter)],
    provider: Annotated[OAuthProvider, Path(description="wechat | qq | github | gitee")],
    code: Annotated[str | None, Query(description="OAuth 授权码")] = None,
    state: Annotated[str | None, Query(description="OAuth 状态参数")] = None,
) -> RedirectResponse:
    fe_fallback = settings.OAUTH_FRONTEND_FALLBACK

    async def resolve_frontend() -> str:
        if not state:
            return fe_fallback
        raw = await RedisCURD(redis).get(f"{STATE_PREFIX}{state}")
        if not raw:
            return fe_fallback
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
            return str(payload.get("frontend_redirect") or fe_fallback).strip() or fe_fallback
        except json.JSONDecodeError:
            return fe_fallback

    if provider not in {"wechat", "qq", "github", "gitee"}:
        url = oauth_service_error_redirect(await resolve_frontend(), "不支持的 OAuth 渠道")
        return RedirectContentResponse(url=url, status_code=302)
    if not code or not state:
        url = oauth_service_error_redirect(await resolve_frontend(), "授权被取消或参数不完整")
        return RedirectContentResponse(url=url, status_code=302)
    try:
        token, fe = await complete_oauth_login(
            request=request,
            redis=redis,
            db=db,
            provider=provider,
            code=code,
            state=state,
            background_tasks=background_tasks,
        )
        success_url = oauth_service_frontend_redirect_from_token(fe, token)
        return RedirectContentResponse(url=success_url, status_code=302)
    except CustomException as e:
        fe = await resolve_frontend()
        return RedirectContentResponse(url=oauth_service_error_redirect(fe, e.msg), status_code=302)


# =================================================== #
# *************** 微信小程序登录端点 ***************** #
# =================================================== #


@AuthRouter.post("/wx-login", summary="微信小程序登录", response_model=ResponseSchema[LoginOutSchema])
async def wx_mini_login_controller(
    request: Request,
    redis: Annotated[Redis, Depends(redis_getter)],
    db: Annotated[AsyncSession, Depends(db_getter)],
    body: WxLoginSchema,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """微信小程序登录（code2Session）。

    前端通过 uni.login 获取 code，后端调用微信 code2Session 接口换取 openid，
    然后查找或自动注册用户，最终返回 JWT。
    """
    session_data = await code2session(code=body.code)
    openid = session_data["openid"]

    user = await ensure_wx_user(
        db=db,
        openid=openid,
        nickname=body.nickname,
        avatar=body.avatar,
    )

    if user.status == 1:
        raise CustomException(msg="用户已被停用")

    user = await UserCRUD(AuthSchema(), db).update_last_login(id=user.id)
    if not user:
        raise CustomException(msg="用户不存在")

    token = await LoginService.create_token(
        request=request,
        redis=redis,
        user=user,
        login_type="wx_mini",
        background_tasks=background_tasks,
    )

    user_info = {
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "avatar": user.avatar,
        "is_superuser": user.is_superuser,
    }

    logger.info(f"微信小程序用户登录成功: {user.username}")

    return SuccessResponse(
        data=LoginOutSchema(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_in=token.expires_in,
            token_type=token.token_type,
            user_info=user_info,
        ),
        msg="登录成功",
    )


@AuthRouter.post("/wx-phone-login", summary="微信小程序手机号登录", response_model=ResponseSchema[LoginOutSchema])
async def wx_mini_phone_login_controller(
    request: Request,
    redis: Annotated[Redis, Depends(redis_getter)],
    db: Annotated[AsyncSession, Depends(db_getter)],
    body: WxPhoneLoginSchema,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """微信小程序手机号快速登录。

    前端 <button open-type="getPhoneNumber"> 回调 e.detail.code，
    后端调用微信 getuserphonenumber 接口获取手机号，
    然后通过手机号查找用户；如果不存在则自动注册。

    注意：此接口需要先调用 uni.login 获取 session，
    前端在 getPhoneNumber 回调中会同时获得 code（用于换取手机号）。
    """
    phone = await get_phone_number(redis=redis, code=body.code)

    # 通过手机号查找已有用户
    auth = AuthSchema()
    user = await UserCRUD(auth, db).get(mobile=phone)

    if not user:
        # 未找到用户 → 自动注册（用手机号生成用户名）
        username = f"wxphone_{phone[-4:]}_{secrets.token_hex(4)}"
        # 确保用户名以字母开头
        if not username[0].isalpha():
            username = "w" + username
        username = username[:32]

        reg = UserCreateSchema(
            username=username,
            password=secrets.token_urlsafe(24),
            name=f"用户{phone[-4:]}",
            mobile=phone,
            role_ids=list(settings.OAUTH_DEFAULT_ROLE_IDS),
        )
        try:
            await UserService(auth, db).create(data=reg)
        except Exception:
            raise CustomException(msg="手机号用户注册失败")

        user = await UserCRUD(auth, db).get(mobile=phone)
        if not user:
            raise CustomException(msg="手机号用户注册失败")
        logger.info(f"微信手机号自动注册用户: {username}")

    if user.status == 1:
        raise CustomException(msg="用户已被停用")

    user = await UserCRUD(auth, db).update_last_login(id=user.id)
    if not user:
        raise CustomException(msg="用户不存在")

    token = await LoginService.create_token(
        request=request,
        redis=redis,
        user=user,
        login_type="wx_mini_phone",
        background_tasks=background_tasks,
    )

    user_info = {
        "id": user.id,
        "username": user.username,
        "name": user.name,
        "avatar": user.avatar,
        "is_superuser": user.is_superuser,
        "mobile": user.mobile,
    }

    logger.info(f"微信手机号用户登录成功: {user.username}")

    return SuccessResponse(
        data=LoginOutSchema(
            access_token=token.access_token,
            refresh_token=token.refresh_token,
            expires_in=token.expires_in,
            token_type=token.token_type,
            user_info=user_info,
        ),
        msg="登录成功",
    )


@AuthRouter.post("/wx-qrcode/generate", summary="生成小程序码", response_model=ResponseSchema[WxQrCodeOutSchema])
async def wx_qrcode_generate_controller(
    redis: Annotated[Redis, Depends(redis_getter)],
    body: WxQrCodeSchema,
) -> JSONResponse:
    """生成无限制小程序码。

    调用微信 getwxacodeunlimit 接口生成小程序码图片，
    返回 base64 编码的图片数据，前端可直接用于 Canvas 绘制或显示。
    """
    import base64

    image_bytes = await get_qrcode(
        redis=redis,
        scene=body.scene,
        page=body.page,
        width=body.width,
    )

    # 转 base64 data URI，前端可直接作为图片 src 使用
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_uri = f"data:image/png;base64,{b64}"

    return SuccessResponse(
        data=WxQrCodeOutSchema(url=data_uri),
        msg="生成成功",
    )
