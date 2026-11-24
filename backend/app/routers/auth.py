"""Authentication endpoints."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response

from .. import (
    captcha,
    crypto,
    emailcodes,
    i18n,
    invites,
    loginalert,
    mailer,
    maintenance,
    panel_url,
    password_reset,
    prefs,
    quota,
    regcodes,
    security,
    store,
    throttle,
    totp,
    trusteddevices,
)
from ..config import settings
from ..schemas import (
    ForgotPasswordIn,
    LoginMfaRequest,
    LoginRequest,
    LoginResponse,
    PasswordChange,
    ProfileUpdate,
    RegisterEmailCodeIn,
    RegisterRequest,
    ResetPasswordIn,
    ResetTokenIn,
    StepUpRequest,
    TotpCodeIn,
    TotpDisableIn,
    UserOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 注册 / 找回密码都是公开的匿名入口，放开手会被脚本拿来做 CPU 放大（注册要跑
# bcrypt）或刷邮件（SMTP 配额有限）。限速计数落 MySQL（throttle.py），重启不清零。
_LIMIT_WINDOW = 3600


async def _anon_throttled(kind: str, ip: str, limit: int) -> bool:
    """匿名入口的 IP 限速：窗口内超过 ``limit`` 次即拒。"""
    return await _hit(f"{kind}:ip:{ip or 'unknown'}", limit)


async def _hit(key: str, limit: int) -> bool:
    """通用固定窗口计数：超过 ``limit`` 次即返回 True（已超限）。"""
    allowed, _ = await throttle.allow(key, limit, _LIMIT_WINDOW)
    return not allowed


async def _check_captcha(
    *,
    captcha_id: str,
    captcha_code: str,
    captcha_x: Optional[float],
    captcha_path: Optional[List[Dict[str, float]]],
) -> Tuple[bool, str, str]:
    """统一的人机校验入口：返回 ``(是否通过, 失败提示, 审计短句)``。

    登录 / 注册 / 忘记密码 / 重置密码四个匿名入口共用同一套「设置 → 登录验证」
    配置。抽成一个函数不只是去重：这四个入口**必须**口径一致，曾经就是漏了
    重置密码这一步（滑块模式下那个页面根本没有验证码），只补一处实现迟早
    还会在第五个入口重演。

    通过时后两项为空串；未通过时提示面向用户（区分「答错」与「已失效」），
    短句面向审计日志（写成人话，如「拖动滑块未通过」）。
    """
    mode = await captcha.get_mode()
    if mode == captcha.MODE_OFF:
        return True, "", ""
    if mode == captcha.MODE_SLIDER:
        # 轨迹一并校验：只交坐标、不交轨迹的脚本在这一步就会被拒（H-1）
        state = captcha.check_slider(captcha_id, captcha_x, captcha_path)
    else:
        state = captcha.check(captcha_id, captcha_code)
    if state == captcha.RESULT_OK:
        return True, "", ""

    # 两种失败分开说：答错 → 换一张重试；已失效（过期 / 同一 id 二次提交）
    # → 明确告知「请重新获取」。既不作废前又不含糊其辞（审计项 PT-09）。
    label = captcha.MODE_LABEL.get(mode, mode)
    if state == captcha.RESULT_GONE:
        return False, "验证码已失效，请重新获取", f"{label}已失效（过期或重放）"
    if mode == captcha.MODE_SLIDER:
        return False, "验证失败，请重新拖动滑块", f"{label}未通过"
    return False, "验证码错误，请点击图片更换", f"{label}未通过"


async def _login_guard(username: str, ip: str) -> None:
    """锁定期内直接拒绝，连 bcrypt 都不跑。"""
    locked = await throttle.login_locked(username, ip)
    if locked:
        await store.add_audit(
            username=username,
            action="auth.login",
            result="failed",
            detail=f"账号或来源 IP 处于锁定状态，剩余 {locked} 秒",
            ip=ip,
        )
        raise HTTPException(
            status_code=429,
            detail=(
                f"登录失败次数过多，已临时锁定，请 {max(1, locked // 60)} 分钟后再试"
            ),
            headers={"Retry-After": str(locked)},
        )


async def _login_count_failure(username: str, ip: str, detail: str) -> None:
    """记一次失败；刚好触发锁定时，审计里留下明确一条。"""
    locked = await throttle.login_failed(username, ip)
    await store.add_audit(
        username=username,
        action="auth.login",
        result="failed",
        detail=detail,
        ip=ip,
    )
    if locked:
        await store.add_audit(
            username=username,
            action="auth.login_lockout",
            target=username,
            result="failed",
            detail=(
                f"连续失败达到 {settings.login_max_failures} 次，"
                f"锁定 {settings.login_lockout_minutes} 分钟（来源 IP：{ip}）"
            ),
            ip=ip,
        )


async def _issue_session(
    response: Response,
    user: Dict[str, Any],
    request: Request,
) -> str:
    """签发 access token + refresh cookie，并登记一台设备（一个 jti）。

    返回 access token；``refresh_tokens`` 表里的那一行就是「已登录设备」，
    撤销它（或把 token_version +1）即可把这次登录踢下线。
    """
    version = int(user.get("token_version", 0) or 0)
    # 先建会话拿到 jti，再把它写进 access token 的 sid —— 这样撤销会话就等于
    # 撤销这枚 access token，登出/踢设备立刻生效。
    refresh, jti, expires_at = security.create_refresh_token(user["username"], version)
    await store.register_refresh_token(
        jti,
        user["username"],
        expires_at,
        ip=security.client_ip(request),
        user_agent=request.headers.get("user-agent", ""),
    )
    # 并发会话上限：超过就踢掉最旧的那几台（当前这枚一定在 keep 内）。
    # 不这样做的话，知道口令的人可以无限累积有效期 7 天的会话，而用户
    # 只有在「登录设备」里一条条手动踢（BUG-019）。
    try:
        trimmed = await store.trim_refresh_tokens(
            user["username"], int(settings.session_max_per_user)
        )
    except Exception:  # noqa: BLE001 - 清理失败不能把登录一起弄挂
        logger.warning("修剪旧会话失败，保留本次登录", exc_info=True)
        trimmed = 0
    if trimmed:
        logger.info(
            "账号 %s 的并发会话超过上限，已撤销最旧的 %d 个",
            user["username"],
            trimmed,
        )
    access = security.create_access_token(
        user["username"], user["role"], version, session_id=jti
    )
    security.set_session_cookies(response, access, refresh)
    return access


async def _mark_login(user: Dict[str, Any]) -> None:
    """在管理页留下「最近登录时间」。

    只写一列统计信息：登录本身已经成功，这一步失败（锁等待、连接抖动）不该
    把用户挡在门外，所以异常在这里吞掉、只记一条 warning。
    两步验证的账号在第二步（``/login/2fa``）才算真正登录，那一处同样会调它。
    """
    try:
        await store.touch_last_login(str(user["username"]))
    except Exception:  # noqa: BLE001 - 统计字段写失败不能影响登录
        logger.warning(
            "记录账号 %s 的登录时间失败", user.get("username"), exc_info=True
        )


def _requires_totp_setup(user: Dict[str, Any]) -> bool:
    """该用户被强制要求开 2FA，但还没绑定。"""
    return (
        not user.get("totp_enabled")
        and str(user.get("role") or "") in settings.totp_required_role_list
    )


def _user_out(user: Dict[str, Any]) -> UserOut:
    return UserOut(
        id=user.get("id"),
        username=user["username"],
        role=user["role"],
        email=user.get("email", "") or "",
        enabled=bool(user.get("enabled", True)),
        status=user.get("status", store.STATUS_ACTIVE),
        permissions=security.effective_permissions(user),
        totp_enabled=bool(user.get("totp_enabled")),
        totp_setup_required=bool(user.get("totp_setup_required")),
        expires_at=security.account_expires_at(user),
        # 区域管理员的可管理区域：前端据此决定建机向导里能选哪些区域
        regions=security.user_regions(user),
    )


def _password_weak_reason(password: str) -> Optional[str]:
    """注册 / 改密 / 重置 / 管理员建号共用同一套强度规则。

    规则本体在 :func:`security.password_policy_error`（长度、字母 + 数字、
    常见弱口令），这里只做「返回原因而不是抛异常」的适配。
    """
    return security.password_policy_error(password) or None


async def _panel_url(request: Request) -> str:
    """面板自身地址，用来在邮件里给出可点的登录链接。

    优先用管理员在「设置 → 面板全局地址」里配置的对外域名
    （如 ``https://pvecenter.yjscloud.com``）；未配置时才回落到本次请求的
    base_url —— 走反向代理时它常是 ``localhost``，收件人打不开。
    """
    return await panel_url.resolve(request)


async def _notify_admins_registration(
    username: str, email: str, ip: str, request: Request
) -> str:
    """把新注册申请邮件通知管理员。

    返回一句可供审计的中文说明。**邮件不是关键路径**：没配置或发失败都只
    记录原因，绝不让注册本身失败 —— 否则邮件服务器不可达就会导致没人能注册。
    """
    cfg = await mailer.load_mail()
    if not mailer.is_configured(cfg):
        return "邮件通知未配置，已跳过"
    subject, text, html = mailer.registration_mail(
        username, email, ip, await _panel_url(request)
    )
    ok, detail = await mailer.send_to_admins(subject, text, html=html)
    return detail if ok else f"通知管理员失败：{detail}"


@router.get("/captcha")
async def get_captcha(request: Request) -> Dict[str, Any]:
    """登录页验证。三种方式：关闭 / 图形验证码 / 拖动滑块。

    ``mode`` 一并返回，前端据此决定渲染输入框、滑块还是什么都不渲染 ——
    拿到挑战再回头问一次「这是什么模式」会让登录页多一次往返。
    关闭时只有 ``{"required": false, "mode": "off"}``。

    按来源 IP 令牌桶限流（BUG-002）：签发一张图要跑一次绘图/随机题，滑块也要
    生成题目；接口又必须匿名开放，不设上限就是一条现成的 CPU 放大链。桶容量
    与恢复速度都取 ``CAPTCHA_RATE_LIMIT_PER_MINUTE``（默认 6）：允许登录页正常
    换图，脚本连刷第 7 次开始就 429，且窗口边界不会攒满再放。计数落统一限流表
    （重启不清零、多进程一致），且在读取模式**之前**，关闭验证码时也照样限。
    """
    ip = security.client_ip(request) or "unknown"
    limit = max(1, int(settings.captcha_rate_limit_per_minute))
    allowed, retry_after = await throttle.allow_token(
        f"auth:captcha:ip:{ip}",
        capacity=limit,
        per_minute=limit,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail=i18n.pick(
                "验证码请求过于频繁，请稍后再试",
                "Too many captcha requests. Please try again later.",
            ),
            headers={"Retry-After": str(max(1, retry_after))},
        )

    mode = await captcha.get_mode()
    if mode == captcha.MODE_OFF:
        return {"required": False, "mode": mode}
    if mode == captcha.MODE_SLIDER:
        return {"required": True, "mode": mode, **captcha.issue_slider()}
    return {"required": True, "mode": captcha.MODE_IMAGE, **captcha.issue()}


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest, request: Request, response: Response
) -> LoginResponse:
    ip = security.client_ip(request)

    # 锁定检查放在最前面：被锁就直接拒，不再跑验证码与 bcrypt（这两个都不便宜），
    # 也不会再给爆破脚本任何反馈。
    await _login_guard(payload.username, ip)

    # 验证码放在校验密码之前：画图与比对都便宜，bcrypt 才贵——
    # 脚本摸不到「密码对不对」的信号，就得先老老实实解验证码。
    #
    # 验证码没过**不**计入失败次数：否则拿一张错图（或一个错误位置）反复提交
    # 就能把任意账号锁死（拿别人的用户名做拒绝服务）。计数只认「口令不对」。
    passed, detail, audit_note = await _check_captcha(
        captcha_id=payload.captcha_id,
        captcha_code=payload.captcha_code,
        captcha_x=payload.captcha_x,
        captcha_path=payload.captcha_path,
    )
    if not passed:
        await store.add_audit(
            username=payload.username,
            action="auth.login",
            result="failed",
            # 写成人话（「图形验证码未通过」/「拖动滑块未通过」）：
            # 审计日志是给人看的，写 image/slider 还得回头翻代码。
            detail=audit_note,
            ip=ip,
        )
        raise HTTPException(status_code=400, detail=detail)

    user = await store.get_user(payload.username)

    # Uniform error message so the response cannot be used to enumerate users.
    if not user or not security.verify_password(payload.password, user["password_hash"]):
        await _login_count_failure(payload.username, ip, "用户名或密码错误")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    # 注册审批：密码是对的，但还没被管理员放行。这两种情况分开提示，
    # 否则用户只会以为是密码错了，反复重试。
    status = user.get("status", store.STATUS_ACTIVE)
    if status != store.STATUS_ACTIVE:
        pending = status == store.STATUS_PENDING
        detail = (
            "账号正在等待管理员审批"
            if pending
            else "注册申请未通过，请联系管理员"
        )
        await store.add_audit(
            username=payload.username,
            action="auth.login",
            result="failed",
            detail=detail,
            ip=security.client_ip(request),
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "账号正在等待管理员审批，通过后即可登录"
                if pending
                else "注册申请未通过审批，请联系管理员"
            ),
        )

    if not user.get("enabled", True):
        await store.add_audit(
            username=payload.username,
            action="auth.login",
            result="failed",
            detail="账号已被禁用",
            ip=security.client_ip(request),
        )
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")

    # 有效期：口令校验之后再判，否则「已冻结」会成为一枚账号是否存在的探测口。
    # 到期后账号保持原样（不 disable），管理员延长有效期即可恢复 —— 冻结只是
    # 一道按时间生效的开关，不需要人来改 enabled。
    if security.is_account_expired(user):
        detail = i18n.t("error.account_frozen")
        await store.add_audit(
            username=payload.username,
            action="auth.login",
            result="failed",
            detail=detail,
            ip=security.client_ip(request),
        )
        raise HTTPException(status_code=403, detail=detail)

    # 维护模式：对外暂停服务，只放管理员进来。
    # 放在口令校验**之后**：登录页本身会先读公开的维护状态并给出提示，这里只是
    # 「确实别让他登进来」的最后一道；也不必让未认证的请求靠这条分支去探面板状态。
    if not security.is_admin(user):
        state = await maintenance.state()
        if state["enabled"]:
            await store.add_audit(
                username=str(user["username"]),
                action="auth.login",
                result="failed",
                detail="维护模式：仅管理员可登录",
                ip=ip,
            )
            raise maintenance.error(state)

    # 两步验证：口令对了，但还不算登录成功 —— 先发一枚只能走完这次验证的
    # 临时凭据，避免把 access token 直接交给只掌握了密码的一方。
    #
    # 例外：这台设备在信任期内（当初登录时勾了「信任设备」）。
    # 跳过的**只是第二因子**，口令仍然必须在上面验过 —— 信任设备不是免密登录。
    # 判定全在服务端（见 app.trusteddevices）：cookie 里只是一枚随机口令，
    # 客户端伪造不出「可信」这个结论。
    trusted_device = False
    if user.get("totp_enabled"):
        trusted_device = await trusteddevices.is_trusted(
            request.cookies.get(security.TRUSTED_COOKIE), str(user["username"])
        )
        if not trusted_device:
            version = int(user.get("token_version", 0) or 0)
            await store.add_audit(
                username=user["username"],
                action="auth.login",
                result="mfa_required",
                detail="密码校验通过，等待两步验证",
                ip=ip,
            )
            return LoginResponse(
                mfa_required=True,
                mfa_token=security.create_mfa_token(user["username"], version),
                # 让第二步知道该不该显示「信任设备」勾选框（见 schemas.LoginResponse）
                trust_device_available=bool(settings.totp_trust_enabled),
            )

    # 登录成功：清掉失败计数（账号与 IP 两个维度），再签发会话
    await throttle.clear_login_failures(payload.username, ip)
    access = await _issue_session(response, user, request)
    await _mark_login(user)

    await store.add_audit(
        username=user["username"],
        action="auth.login",
        result="success",
        # 审计里点明「这一步没输动态码，是因为信任设备」：只写 success 的话，
        # 事后翻日志看不出这此登录到底有没有过第二因子。
        detail="两步验证已跳过（信任设备，未过期）" if trusted_device else "",
        ip=ip,
    )
    # 首次从某 IP 登录 → 异步告警管理员（站内消息 + 邮件）。内部吞掉所有异常，
    # 不会影响这次登录；HTTP 响应也不等 SMTP。
    await loginalert.note_login(
        user, request, ip, "trusted_device" if trusted_device else "password"
    )

    return LoginResponse(
        access_token=access,
        token_type="bearer",
        user=_user_out(user),
        totp_setup_required=_requires_totp_setup(user),
    )


@router.post("/login/2fa", response_model=LoginResponse)
async def login_mfa(
    payload: LoginMfaRequest, request: Request, response: Response
) -> LoginResponse:
    """登录第二步：动态码或一次性恢复码换正式令牌。

    动态码同样会被爆破（6 位数字），所以复用登录失败计数与锁定：这里错了
    一样会累计、一样会锁。
    """
    ip = security.client_ip(request)
    mfa = security.decode_token(payload.mfa_token, expected_typ="mfa")
    username = str(mfa.get("sub") or "")
    if not username:
        raise HTTPException(status_code=401, detail="验证已失效，请重新登录")

    await _login_guard(username, ip)

    user = await store.get_user(username)
    if not user or not user.get("totp_enabled"):
        raise HTTPException(
            status_code=401, detail="验证已失效，请重新登录"
        )
    if int(mfa.get("ver", 0)) != int(user.get("token_version", 0) or 0):
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    if user.get("status", store.STATUS_ACTIVE) != store.STATUS_ACTIVE:
        raise HTTPException(status_code=403, detail="账号未通过管理员审批")
    if not user.get("enabled", True):
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")
    if security.is_account_expired(user):
        raise HTTPException(
            status_code=403, detail=i18n.t("error.account_frozen")
        )
    # 维护模式：别让人白跑一趟两步验证（码用过一次就废了）
    if not security.is_admin(user):
        state = await maintenance.state()
        if state["enabled"]:
            raise maintenance.error(state)

    ok, used_recovery = await totp.verify_login_code(user, payload.code)
    if not ok:
        await _login_count_failure(username, ip, "两步验证码错误")
        raise HTTPException(
            status_code=401, detail="验证码不正确，请重新获取后再试"
        )

    await throttle.clear_login_failures(username, ip)
    access = await _issue_session(response, user, request)
    await _mark_login(user)

    # 勾了「信任设备」：登记一台信任设备并下发 cookie。之后 7 天内，这台浏览器
    # 只要口令对就能直接登录（第二因子被跳过），到期或换密码 / 开关 2FA 即失效。
    #
    # 只在**走到这一步**（动态码或恢复码已验过）才签发 —— 放在前面等于没验证就发。
    if payload.trust_device and settings.totp_trust_enabled:
        plain, expires_at = await trusteddevices.remember(
            username,
            ip=ip,
            user_agent=request.headers.get("user-agent", ""),
        )
        security.set_trusted_cookie(
            response, plain, max_age=int(expires_at - time.time())
        )
        await security.audit(
            request,
            user,
            "auth.trust_device",
            detail=f"信任本设备 {trusteddevices.TRUST_DAYS} 天（免两步验证）",
        )

    await store.add_audit(
        username=username,
        action="auth.login",
        result="success",
        detail="两步验证通过" + ("（使用了恢复码）" if used_recovery else ""),
        ip=ip,
    )
    await loginalert.note_login(
        user, request, ip, "recovery_code" if used_recovery else "2fa"
    )
    return LoginResponse(access_token=access, token_type="bearer", user=_user_out(user))


@router.post("/refresh", response_model=LoginResponse)
async def refresh_session(request: Request, response: Response) -> LoginResponse:
    """用 HttpOnly 里的 refresh token 换一枚新的 access token。

    每次刷新都**轮换** refresh token（旧 jti 立刻作废）：一枚被偷走的 refresh
    token 只能用一次，之后要么失效、要么被服务端发现。
    """
    token = request.cookies.get(security.REFRESH_COOKIE, "")
    if not token:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")

    payload = security.decode_token(token, expected_typ="refresh")
    jti = str(payload.get("jti") or "")
    record = await store.get_refresh_token(jti)
    if not record:
        security.clear_session_cookies(response)
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")

    username = str(payload.get("sub") or "")
    user = await store.get_user(username)
    if not user or not user.get("enabled", True):
        await store.revoke_refresh_token(jti)
        security.clear_session_cookies(response)
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    if user.get("status", store.STATUS_ACTIVE) != store.STATUS_ACTIVE:
        await store.revoke_refresh_token(jti)
        security.clear_session_cookies(response)
        raise HTTPException(status_code=403, detail="账号未通过管理员审批")
    # 有效期已过：刷新令牌一并作废，别让浏览器靠 refresh 无限续命。
    if security.is_account_expired(user):
        await store.revoke_refresh_token(jti)
        security.clear_session_cookies(response)
        raise HTTPException(
            status_code=403, detail=i18n.t("error.account_frozen")
        )
    if int(payload.get("ver", 0)) != int(user.get("token_version", 0) or 0):
        await store.revoke_refresh_token(jti)
        security.clear_session_cookies(response)
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")
    # 维护模式：普通用户不续期。刻意不清 cookie —— 维护结束后这次登录还能用，
    # 不必让所有人重输密码；管理员则完全不受影响。
    if not security.is_admin(user):
        state = await maintenance.state()
        if state["enabled"]:
            raise maintenance.error(state)

    await store.revoke_refresh_token(jti)
    access = await _issue_session(response, user, request)
    return LoginResponse(
        access_token=access,
        token_type="bearer",
        user=_user_out(user),
        totp_setup_required=_requires_totp_setup(user),
    )


def _register_closed_message() -> str:
    """注册关闭时的统一文案：接口拒绝与前端关闭提示共用一套口径。"""
    return i18n.pick(
        "自助注册已关闭，请联系管理员开通账号",
        "Self-registration is disabled. Please contact an administrator for an account.",
    )


def _credential_missing_message(kind: str) -> str:
    """邀请制 / 注册码制下没填凭证。"""
    if kind == regcodes.KIND_INVITE:
        return i18n.pick("请填写邀请码", "Please enter your invitation code")
    return i18n.pick("请填写注册码", "Please enter your registration code")


def _credential_invalid_message(kind: str) -> str:
    """凭证不可用：不存在 / 已过期 / 已用完 / 已撤销 / 类型不匹。

    刻意**不区分**这几种情况：注册是匿名接口，区分了就等于给枚举者一个
    「这枚码存在但过期了」的确认信号。
    """
    if kind == regcodes.KIND_INVITE:
        return i18n.pick(
            "邀请码无效、已过期或已用完，请联系管理员重新获取",
            "The invitation code is invalid, expired or used up. Ask an administrator for a new one.",
        )
    return i18n.pick(
        "注册码无效、已过期或已用完，请联系管理员重新获取",
        "The registration code is invalid, expired or used up. Ask an administrator for a new one.",
    )


async def _register_email_code_required(mode: str) -> bool:
    """自助注册是否要邮箱验证码。

    只对 ``open`` 生效：邀请制 / 注册码制的凭证本身就是管理员线下分发的，
    拿到凭证已经等于「管理员认得这个人」，再要一道邮箱码就是过两道门。
    自助注册才是完全匿名的入口 —— 那里唯一能证明「这个邮箱是你的」的东西
    就是这封邮件。

    开关本身由设置页控制（见 :func:`app.emailcodes.enabled`），环境变量只是
    没配过时的默认值。
    """
    return mode == regcodes.MODE_OPEN and await emailcodes.enabled()


def _email_code_send_message(status: str, meta: Dict[str, Any]) -> Tuple[int, str]:
    """把签发结果翻成 ``(HTTP 状态码, 面向用户的说明)``。"""
    ttl_min = max(1, int(meta.get("ttl_seconds", 600)) // 60)
    if status == emailcodes.SEND_COOLDOWN:
        return 429, i18n.pick(
            f"发送过于频繁，请 {max(1, int(meta.get('retry_after', 60)))} 秒后再试",
            f"Too many requests. Try again in {max(1, int(meta.get('retry_after', 60)))} second(s).",
        )
    if status == emailcodes.SEND_RATE_LIMITED:
        return 429, i18n.pick(
            "该邮箱或来源 IP 获取验证码的次数过多，请稍后再试",
            "Too many verification codes requested for this email or IP. Try again later.",
        )
    if status == emailcodes.SEND_MAIL_FAILED:
        return 503, i18n.pick(
            "验证码邮件发送失败，请稍后再试；如果一直失败请联系管理员检查邮件配置",
            "The verification email could not be sent. Try again later, or ask an "
            "administrator to check the mail settings.",
        )
    return 200, i18n.pick(
        f"验证码已发送到 {meta.get('email', '')}，{ttl_min} 分钟内有效",
        f"A verification code was sent to {meta.get('email', '')}; it is valid for {ttl_min} minute(s).",
    )


def _email_code_reject_message(result: str, meta: Dict[str, Any]) -> str:
    """校验失败时的四种说法。

    刻意区分「填错还几次」「已过期」「次数用尽」「压根没发」：它们是四种不同的
    下一步动作（继续填 / 重新获取 / 联系管理员 / 先点发送），统一成一句
    「验证码无效」只会让用户反复试同一个码。
    """
    if result == emailcodes.RESULT_EXPIRED:
        return i18n.pick(
            "邮箱验证码已过期，请重新获取",
            "The email verification code has expired. Request a new one.",
        )
    if result == emailcodes.RESULT_TOO_MANY:
        return i18n.pick(
            "邮箱验证码错误次数过多，已作废，请重新获取",
            "Too many wrong attempts. The code is no longer valid; request a new one.",
        )
    if result == emailcodes.RESULT_INVALID:
        left = int(meta.get("remaining_attempts", 0))
        return i18n.pick(
            f"邮箱验证码不正确，还可尝试 {left} 次",
            f"The email verification code is incorrect. {left} attempt(s) left.",
        )
    return i18n.pick(
        "请先获取邮箱验证码，并填写收到的 6 位数字",
        "Request an email verification code first, then enter the 6-digit code you received.",
    )


@router.get("/registration-mode")
async def registration_mode() -> Dict[str, Any]:
    """当前注册方式（公开接口，无需登录）。

    登录页据此决定是否展示「注册申请」入口，注册页据此决定渲染哪个凭证输入框。
    模式存在 settings KV（管理员在「设置 → 注册方式」里切换），读取没有副作用，
    所以不需要限流 —— 但它必须**每次都查库**，否则「切换后立即生效」就成了空话。
    """
    mode = await regcodes.current_mode()
    kind = regcodes.MODE_KIND.get(mode) or ""
    if mode == regcodes.MODE_OFF:
        message = _register_closed_message()
    elif mode == regcodes.MODE_INVITE:
        # 邀请制没有「缺失凭证」这回事：入口是链接，注册页据 mode 直接提示
        message = "邀请制仅支持通过邀请链接注册，请向管理员索取邀请链接"
    elif kind:
        message = _credential_missing_message(kind)
    else:
        message = ""
    return {
        "mode": mode,
        "requires": kind,
        "enabled": mode != regcodes.MODE_OFF,
        "message": message,
        # 自助注册的邮箱验证码：注册页据此决定是否渲染验证码输入框与「发送」按钮，
        # 并直接拿这里的秒数做倒计时与提示（不在前端重复一份配置）。
        "email_code": {
            "required": await _register_email_code_required(mode),
            "ttl_seconds": emailcodes.ttl_seconds(),
            "max_attempts": emailcodes.max_attempts(),
            "resend_after": emailcodes.resend_after(),
        },
    }


@router.get("/register-availability")
async def register_availability() -> Dict[str, Any]:
    """注册是否开放（公开接口）—— ``/registration-mode`` 的兼容视图。

    老前端只认 ``enabled``；保留这条是为了升级过程中「前端还是旧包、后端已
    更新」的那段时间不出现入口消失/报错。
    """
    mode = await regcodes.current_mode()
    enabled = mode != regcodes.MODE_OFF
    return {
        "enabled": enabled,
        "message": "" if enabled else _register_closed_message(),
    }


@router.get("/invite/{token}")
async def invite_preview(token: str, request: Request) -> Dict[str, Any]:
    """邀请链接的公开预览（注册页打开 ``?invite=`` 时先问一句）。

    返回「还能不能用、进来是什么角色、要不要审批、还剩几次」。链接无效时也回
    200 + ``valid: false`` 而不用 404：注册页要能稳稳地渲染一句「这个链接不能
    用了」，用状态码区分会让前端多一层错误分支，也容易把提示写丢。

    token 是 43 字符的高熵串，猜不出来；仍然按 IP 限流，避免有人拿它当免费
    的哈希查询接口刷着玩。
    """
    ip = security.client_ip(request) or "unknown"
    if await _anon_throttled("invite-preview", ip, 30):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")
    # 链接归属邀请制：其它注册方式下链接本来就不生效，预览直接说不能用，
    # 省得注册页显示「可以加入」却在提交时被拒。
    mode = await regcodes.current_mode()
    if mode != regcodes.MODE_INVITE:
        return {
            "valid": False,
            "reason": "当前注册方式不是邀请制，邀请链接不可用",
        }
    row = await invites.peek(token)
    if row is None:
        return {
            "valid": False,
            "reason": "邀请链接无效、已失效或使用次数已用完",
        }
    role = await store.get_role(row["role"])
    if not role:
        # 链接指向的角色被删了：注册必然失败，预览就先说不能用，
        # 别让人填完一屏才在提交时撞到「角色不存在」
        return {
            "valid": False,
            "reason": "该邀请链接指定的角色已不存在，请联系管理员",
        }
    return {
        "valid": True,
        "role": row["role"],
        "role_name": str(role.get("name") or row["role"]),
        "requires_approval": bool(row["requires_approval"]),
        "remaining": row["remaining"],
        "expires": row["expires"],
        "note": row["note"],
    }


def _registration_received(
    status: str = store.STATUS_PENDING,
    message: str = "注册申请已收到，请等待管理员审批",
) -> Dict[str, Any]:
    """注册的对外统一响应。

    需要审批的通道里，首次提交与「用户名已存在」都走这里：body / 状态码完全
    一致，调用方无法用它判断账号是否存在（审计项 M-02）。免审批凭证不走这条
    伪装逻辑：用户名已存在时明确返回 409，否则用户会把重复提交误认为「等待
    审批」，永远等不到一个不存在的审批。
    """
    return {
        "ok": True,
        "status": status,
        "message": message,
    }


@router.post("/register/email-code")
async def send_register_email_code(
    payload: RegisterEmailCodeIn, request: Request
) -> Dict[str, Any]:
    """自助注册第 1 步：把一次性验证码发到用户填写的邮箱。

    与第 2 步（``/register``）分开是刻意的：邮箱是**匿名接口里唯一能证明归属的
    东西**，而「发信」本身是个对外投递动作，必须单独限频、单独要求人机校验，
    不能和提交绑在一起 —— 否则脚本一次请求就能既发信又建号。

    人机校验在这里（而不是提交时）生效：提交端已经有验证码作为「你是不是这个
    邮箱的主人」的证明，再要一道图形码只会让人过两次题。
    """
    ip = security.client_ip(request)
    mode = await regcodes.current_mode()
    if mode == regcodes.MODE_OFF:
        raise HTTPException(status_code=403, detail=_register_closed_message())
    if not await _register_email_code_required(mode):
        raise HTTPException(
            status_code=400,
            detail=i18n.pick(
                "当前注册方式不需要邮箱验证码",
                "The current registration mode does not use email verification codes.",
            ),
        )

    state = await maintenance.state()
    if state["enabled"]:
        raise maintenance.error(state)

    # 发信接口按 IP 再限一层：单邮箱上限挡不住「换着邮箱群发」。
    if await _anon_throttled("register-code", ip or "unknown", 10):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试")

    passed, detail, audit_note = await _check_captcha(
        captcha_id=payload.captcha_id,
        captcha_code=payload.captcha_code,
        captcha_x=payload.captcha_x,
        captcha_path=payload.captcha_path,
    )
    if not passed:
        await store.add_audit(
            username="",
            action="auth.register.email_code",
            target="",
            result="failed",
            detail=audit_note,
            ip=ip,
        )
        raise HTTPException(status_code=400, detail=detail)

    email = (payload.email or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="请填写正确的邮箱")
    if len(email) > 255:
        raise HTTPException(status_code=400, detail="邮箱长度不能超过 255 个字符")

    status, meta = await emailcodes.issue(email, ip or "")
    http_status, message = _email_code_send_message(status, meta)
    await store.add_audit(
        username="",
        action="auth.register.email_code",
        target=email,
        result="success" if http_status == 200 else "failed",
        detail=message if http_status != 200 else "",
        ip=ip,
    )
    if http_status != 200:
        headers = (
            {"Retry-After": str(max(1, int(meta.get("retry_after") or 1)))}
            if http_status == 429
            else None
        )
        raise HTTPException(status_code=http_status, detail=message, headers=headers)

    return {
        "ok": True,
        "message": message,
        "ttl_seconds": meta["ttl_seconds"],
        "max_attempts": meta["max_attempts"],
        "resend_after": meta["resend_after"],
    }


@router.post("/register", status_code=201)
async def register(payload: RegisterRequest, request: Request) -> Dict[str, Any]:
    """自助注册（公开接口，无需登录）。

    建出来的账号默认是 ``pending``：能登录的前提是管理员审批通过。例外是
    「免审批」的凭证（邀请链接 / 注册码都可以在签发时关掉审批）—— 那条路径
    建好即 ``active``，角色当场生效。角色默认 viewer；带凭证时用凭证上配置的。

    校验顺序是「注册方式 → 维护模式 → 限流 → 人机校验（自助注册换成邮箱验证码）
    → 口令强度 → 邮箱 → 凭证（邀请制 / 注册码制）→ 域名限速 → 队列容量 →
    用户名唯一 → 邮箱验证码 → 占用凭证 → 建号」，任何一步失败都**不写用户表**，
    也不给管理员发信：这样「先插入待审批记录、再校验重复」那类问题不会出现
    （首投成功 = 201 且队列里一条 pending；同名重投同样是 201 +「已收到」，
    队列不受影响）。

    邮箱验证码排得靠后是有意的：前面几种拒绝（口令太弱、重名、队列满）都是
    「改一下就能重试」的，不该把用户手上那枚码烧掉；而码一旦通过就作废，
    所以它必须在最后一道可能失败的门之前。

    凭证只在「前面的字段都合法」之后才真正占用额度（``consume``）：填错一次
    口令就废掉一枚邀请码，用户还得回去找管理员要新的。建号失败会把额度退回。

    注意这里**不返回任何账号信息**，也不在响应里区分「已存在」的具体状态 ——
    与登录接口的防枚举口径保持一致。
    """
    ip = security.client_ip(request)
    # 注册方式（设置页可切，切换后立即生效）：关闭时直接 403 —— 不消耗限流
    # 配额，也不落审计（匿名可无限触发，记了只会把审计表刷爆）。放在维护模式
    # 之前：关闭是配置级的，维护是临时的，语义更硬的那条先答。
    mode = await regcodes.current_mode()
    if mode == regcodes.MODE_OFF:
        raise HTTPException(status_code=403, detail=_register_closed_message())

    # 邀请链接属于「邀请制」：链接自带角色与审批策略，它本身就是邀请凭证，
    # 所以邀请制下带了链接就不再要求邀请码 —— 再要一枚等于让用户过两道门。
    # 其它方式一律不认链接（它曾经是一种独立入口，现在归属邀请制）。
    invite_token = (payload.invite_token or "").strip()
    if invite_token and mode != regcodes.MODE_INVITE:
        if mode == regcodes.MODE_CODE:
            raise HTTPException(
                status_code=400,
                detail="当前为注册码制，邀请链接不适用，请填写管理员分发的注册码",
            )
        raise HTTPException(
            status_code=400,
            detail="当前为自助注册，无需邀请链接，直接提交即可",
        )
    # 邀请制已只保留邀请链接：没有链接就没有凭证，直接拒绝（不再回落到邀请码）
    if mode == regcodes.MODE_INVITE and not invite_token:
        raise HTTPException(
            status_code=400,
            detail="邀请制仅支持通过邀请链接注册，请向管理员索取邀请链接",
        )
    invite = await invites.peek(invite_token) if invite_token else None
    if invite_token and invite is None:
        raise HTTPException(
            status_code=400,
            detail="邀请链接无效、已失效或使用次数已用完，请向管理员索取新的链接",
        )
    # 角色可能在链接签发之后被删掉：提前拦住，否则要到 create_user 才炸成 500
    if invite is not None and not await store.get_role(invite["role"]):
        raise HTTPException(
            status_code=400, detail="该邀请链接指定的角色已不存在，请联系管理员"
        )

    # 需要凭证的模式 → 认哪一种凭证（邀请制认邀请码，注册码制认注册码）
    need_kind = "" if invite is not None else (regcodes.MODE_KIND.get(mode) or "")
    # 维护模式：自助注册也属于「对外服务」，一并停掉。放在限流之前 —— 维护期间
    # 没有理由再为注册消耗一次限流配额。
    state = await maintenance.state()
    if state["enabled"]:
        raise maintenance.error(state)
    if await _anon_throttled("register", ip, 5):
        await store.add_audit(
            username=payload.username,
            action="auth.register",
            target=payload.username,
            result="failed",
            detail="提交过于频繁，已被限速",
            ip=ip,
        )
        raise HTTPException(
            status_code=429, detail="提交过于频繁，请稍后再试"
        )

    username = payload.username.strip()
    email = (payload.email or "").strip()

    # 自助注册改由邮箱验证码把门（见 app/emailcodes.py）：它同时承担了「人机」
    # 与「邮箱真实且属于你」两件事，比图形码强；再要一道图形码等于让用户过两次
    # 题（发码那一步的图形码已经被消耗掉了）。邀请制 / 注册码制没有人机校验的
    # 替代品，仍然走原来的图灵校验。
    code_required = await _register_email_code_required(mode)

    # 人机校验与登录同级（审计项 FD-06）：注册成功会往审批队列里塞一条记录，
    # 也会给管理员发一封信 —— 没有验证码，匿名脚本可以用随机用户名把队列刷满，
    # 顺带把管理员邮箱打爆。放在建号之前：未通过就什么都不写。
    #
    # 验证码没过**不**计入任何失败计数，理由同登录：否则拿错图反复提交就能把
    # 正常用户挡在门外（限流仍按来源 IP 独立生效）。
    if not code_required:
        passed, detail, audit_note = await _check_captcha(
            captcha_id=payload.captcha_id,
            captcha_code=payload.captcha_code,
            captcha_x=payload.captcha_x,
            captcha_path=payload.captcha_path,
        )
        if not passed:
            await store.add_audit(
                username=payload.username,
                action="auth.register",
                target=payload.username,
                result="failed",
                detail=audit_note,
                ip=ip,
            )
            raise HTTPException(status_code=400, detail=detail)

    weak = _password_weak_reason(payload.password)
    if weak:
        raise HTTPException(status_code=400, detail=weak)
    # 邮箱必填：审批结果要发到这里
    if not email:
        raise HTTPException(
            status_code=400, detail="请填写邮箱，审批结果会发送到该邮箱"
        )
    if "@" not in email:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if len(email) > 255:
        raise HTTPException(status_code=400, detail="邮箱长度不能超过 255 个字符")

    # 邀请制 / 注册码制：先**判活**（不消费）。放在域名限速与队列容量之前 ——
    # 一枚假码不该消耗域名配额，也不该占掉一个待审批名额。
    credential = (payload.invite_code or "").strip()
    credential_label = "邀请码" if need_kind == regcodes.KIND_INVITE else "注册码"
    peeked_code: Optional[Dict[str, Any]] = None
    if need_kind:
        if not credential:
            await store.add_audit(
                username=username,
                action="auth.register",
                target=username,
                result="failed",
                detail=f"未填写{credential_label}（当前注册方式：{mode}）",
                ip=ip,
            )
            raise HTTPException(
                status_code=400, detail=_credential_missing_message(need_kind)
            )
        peeked_code = await regcodes.check(credential, need_kind)
        if peeked_code is None:
            await store.add_audit(
                username=username,
                action="auth.register",
                target=username,
                result="failed",
                detail=f"{credential_label}无效 / 过期 / 已用完（当前注册方式：{mode}）",
                ip=ip,
            )
            raise HTTPException(
                status_code=400, detail=_credential_invalid_message(need_kind)
            )

    # 邮箱域名级限速（审计项 M-02）：只按 IP 限 5 次/小时挡不住换 IP 的脚本，
    # 而同一批灌号往往落在同一个域名（临时邮箱服务 / 同一家企业域）。10 次/小时
    # 是「同一家公司一天内正常申请几个人」也够用的上限。
    domain = email.rsplit("@", 1)[-1].strip().lower()
    if await _hit(f"register:domain:{domain}", 10):
        await store.add_audit(
            username=username,
            action="auth.register",
            target=username,
            result="failed",
            detail=f"邮箱域名 {domain} 注册过于频繁，已被限速",
            ip=ip,
        )
        raise HTTPException(
            status_code=429, detail="该邮箱域名提交过于频繁，请稍后再试"
        )

    # 审批队列容量兜底（审计项 M-02）：即使攻击者换 IP、换域名，也不能让
    # pending 无限堆积把管理员淹没。到上限就自动拒绝并留审计（含来源 IP），
    # 管理员清理后再放行；阈值可用 REGISTER_PENDING_LIMIT 调整。
    pending = await store.count_users_with_status(store.STATUS_PENDING)
    pending_cap = int(settings.register_pending_limit)
    # 免审批凭证不会往队列里加人：队列满时它们不该被一并挡掉。这里用前面
    # 判活的凭证预览（仍需在后面真正 consume），不额外暴露任何信息。
    auto_approved = (
        (invite is not None and not invite.get("requires_approval", True))
        or (peeked_code is not None and not peeked_code.get("requires_approval", True))
    )
    if pending_cap > 0 and pending >= pending_cap and not auto_approved:
        logger.warning(
            "注册审批队列已满（%s 条），拒绝来自 %s 的注册请求：%s",
            pending,
            ip or "-",
            username,
        )
        await store.add_audit(
            username=username,
            action="auth.register",
            target=username,
            result="failed",
            detail=f"待审批队列已满（{pending} 条），自动拒绝",
            ip=ip,
        )
        raise HTTPException(
            status_code=429,
            detail="注册申请暂时无法受理，请稍后再试",
        )

    # 已存在（无论什么状态）都**不透露**：需要审批的通道继续返回与首次提交
    # 完全一致的 pending 响应，否则这个公开接口就是一台用户名枚举机（审计项
    # M-02）。但免审批凭证例外：它不会产生审批，若继续伪装成 pending，用户会
    # 永远等一个不存在的审批。持有效免审批凭证的人本来就能创建账号，这里明确
    # 告知用户名冲突不会额外泄露可利用的信息。
    if await store.get_user(username):
        if auto_approved:
            await store.add_audit(
                username=username,
                action="auth.register",
                target=username,
                result="failed",
                detail="用户名已被占用（免审批凭证，明确拒绝）",
                ip=ip,
            )
            raise HTTPException(
                status_code=409,
                detail="用户名已被占用，请更换用户名；如果你已经注册过，请直接登录或联系管理员处理原账号",
            )

        await store.add_audit(
            username=username,
            action="auth.register",
            target=username,
            result="failed",
            detail="用户名已被占用（对外统一为已收到）",
            ip=ip,
        )
        return _registration_received()

    # 自助注册：邮箱验证码校验放在这里 —— 前面几种拒绝（密码太弱、重名、队列满）
    # 都是「用户改一下就能重试」的，不该把码烧掉；而码一旦通过就作废，所以它必须
    # 排在最后一道可能失败的门之前。校验失败按原因分档提示（见 _email_code_reject_message）。
    if code_required:
        result, code_meta = await emailcodes.verify(email, payload.email_code)
        if result != emailcodes.RESULT_OK:
            await store.add_audit(
                username=username,
                action="auth.register",
                target=username,
                result="failed",
                detail=f"邮箱验证码未通过（{result}）",
                ip=ip,
            )
            raise HTTPException(
                status_code=400, detail=_email_code_reject_message(result, code_meta)
            )

    # 到此为止字段全部合法，才真正占用一次凭证额度（条件 UPDATE，并发不超发）。
    # 建号失败会把额度退回去，不让用户白丢一枚邀请码。
    consumed: Optional[Dict[str, Any]] = None
    if need_kind:
        consumed = await regcodes.consume(
            credential, need_kind, used_by=f"{username} <{email}>"
        )
        if consumed is None:
            await store.add_audit(
                username=username,
                action="auth.register",
                target=username,
                result="failed",
                detail=f"{credential_label}在提交过程中失效（并发用尽 / 被撤销）",
                ip=ip,
            )
            raise HTTPException(
                status_code=400, detail=_credential_invalid_message(need_kind)
            )

    # 链接同样只在字段全部合法之后才扣次数
    consumed_invite: Optional[Dict[str, Any]] = None
    if invite is not None:
        consumed_invite = await invites.consume(
            invite_token, used_by=f"{username} <{email}>"
        )
        if consumed_invite is None:
            await store.add_audit(
                username=username,
                action="auth.register",
                target=username,
                result="failed",
                detail="邀请链接在提交过程中失效（次数用尽 / 被失效）",
                ip=ip,
            )
            raise HTTPException(
                status_code=400,
                detail="邀请链接在提交过程中失效，请向管理员索取新的链接",
            )

    # 角色、额度与审批策略都由凭证决定（注册码与邀请链接都能预先指定）：
    # 免审批凭证建好即可登录（active），其余一律 pending；自助注册没有凭证，
    # 保持原来的「必须审批」。
    invite_role = str((consumed_invite or {}).get("role") or "")
    code_role = str((consumed or {}).get("role") or "")
    assigned_role = invite_role or code_role or "viewer"
    if code_role and not await store.get_role(code_role):
        # 码签发后角色被删：同样提前拦住，别让 create_user 抛成 500
        if consumed is not None:
            await regcodes.release(str(consumed.get("id") or ""))
        raise HTTPException(
            status_code=400, detail="该注册码指定的角色已不存在，请联系管理员"
        )
    limits = (consumed_invite or {}).get("quota") or (consumed or {}).get("quota")
    used_credential = consumed_invite or consumed or {}
    needs_approval = bool(used_credential.get("requires_approval", True))
    status = store.STATUS_PENDING if needs_approval else store.STATUS_ACTIVE

    try:
        await store.create_user(
            username,
            payload.password,
            role=assigned_role,
            email=email,
            status=status,
        )
    except Exception:
        if consumed is not None:
            await regcodes.release(str(consumed.get("id") or ""))
        if consumed_invite is not None:
            await invites.release(str(consumed_invite.get("id") or ""))
        raise

    # 额度写进用户级（覆盖角色默认）。失败不影响注册结果：账号已经建好，
    # 额度只是附加项，记日志让管理员能补设。
    if limits:
        try:
            await quota.set_user_limits(
                username, limits, actor=f"registration:{mode}"
            )
        except Exception:  # noqa: BLE001
            logger.warning("注册时写入用户额度失败：%s", username, exc_info=True)

    # 通知管理员。发信失败只记进审计，不影响注册结果。
    # 免审批凭证不发：那封模板的标题就是「[待审批]」，发出去只会让人误会。
    mail_note = (
        await _notify_admins_registration(username, email, ip, request)
        if needs_approval
        else "免审批凭证，未发送审批通知"
    )

    # 注册是匿名行为，actor 与 target 都是这个新账号本身；
    # 审批队列靠这条记录回溯「谁在什么时候申请过」。
    await store.add_audit(
        username=username,
        action="auth.register",
        target=username,
        result="success",
        detail={
            "email": email,
            "status": status,
            "mail": mail_note,
            # 用哪种方式注册的、哪枚凭证 / 哪条链接：出问题时能回查是哪一批放进来的人
            "mode": mode,
            "code_id": str((consumed or {}).get("id") or ""),
            "invite_id": str((consumed_invite or {}).get("id") or ""),
            "role": assigned_role,
            "quota": bool(limits),
        },
        ip=ip,
    )
    return _registration_received(
        status,
        "注册成功，现在可以直接登录"
        if not needs_approval
        else "注册申请已收到，请等待管理员审批",
    )


# ------------------------------------------------------------ 自助重置密码
async def _deliver_password_reset(
    username: str, to_email: str, url: str, ip: str
) -> None:
    """真正发重置邮件（后台任务，失败只留日志 / 审计）。

    为什么放到响应之后：SMTP 是网络 I/O，存在与否两条路径的耗时差别会被
    当成「账号是否存在」的侧信道（审计项 L-01）。对外统一立刻返回通用话术，
    发信结果只进服务端审计 —— 管理员能看到失败原因，匿名调用者看不到。
    """
    try:
        # 重置邮件按**收件人**的语言渲染，而不是当前请求的语言 —— 两者可能不同：
        # 用户可能在另一台设备上刚切换过语言，或由管理员代触发。
        lang = await prefs.get(username, prefs.PREF_LANGUAGE, i18n.DEFAULT_LANG)
        with i18n.use_language(lang):
            subject, body, html = mailer.password_reset_mail(username, url)
        # 收件人是账号邮箱，不是用户名；用户名投给 SMTP 只会 501 Bad address syntax
        ok, detail = await mailer.send_mail(to_email, subject, body, html=html)
        await store.add_audit(
            username=username,
            action="auth.password_reset.request",
            target=username,
            result="success" if ok else "failed",
            detail=detail,
            ip=ip,
        )
        if not ok:
            logger.warning(
                "密码重置邮件发送失败（账号 %s，来源 %s）：%s", username, ip or "-", detail
            )
    except Exception:  # noqa: BLE001 - 后台任务不能把异常抛回响应
        logger.exception("密码重置邮件投递异常（账号 %s）", username)


@router.post("/forgot-password")
async def forgot_password(
    payload: ForgotPasswordIn,
    request: Request,
    background: BackgroundTasks,
) -> Dict[str, Any]:
    """自助重置第 1 步：给账号邮箱发一条一次性链接。

    **无论账号是否存在、邮箱填没填、SMTP 配没配好，都返回同一句话**。这是
    公开接口，任何差异（状态码、文案、甚至「邮件发送失败」）都是一台账号
    枚举机（审计项 L-01）。
    """
    ip = security.client_ip(request)
    username = (payload.username or "").strip()

    if await _anon_throttled("reset", ip, 5):
        raise HTTPException(status_code=429, detail="提交过于频繁，请稍后再试")

    generic = {
        "ok": True,
        "message": "如果该账号存在且已填写邮箱，重置链接已发送，15 分钟内有效",
    }
    if not username:
        # 空用户名不值得查库，但同样返回通用话术
        return generic

    # 人机校验（审计项 L-01）：与登录 / 注册同一套配置。放在查库之前，
    # 未通过时什么都不发、也不消耗该用户名的发送配额。
    passed, detail, audit_note = await _check_captcha(
        captcha_id=payload.captcha_id,
        captcha_code=payload.captcha_code,
        captcha_x=payload.captcha_x,
        captcha_path=payload.captcha_path,
    )
    if not passed:
        await store.add_audit(
            username=username,
            action="auth.password_reset.request",
            target=username,
            result="failed",
            detail=f"{audit_note}，未发送重置邮件",
            ip=ip,
        )
        raise HTTPException(status_code=400, detail=detail)

    # 单账号的邮件轰炸上限（审计项 L-01）：只按 IP 限速挡不住换 IP 的脚本，
    # 而受害者收到的邮件条数与攻击者 IP 数成正比。3 次/小时足够正常用户
    # （忘了自己申请过 / 换邮箱再试），也不至于把 SMTP 配额打空。
    if await _hit(f"reset:user:{username.lower()}", 3):
        await store.add_audit(
            username=username,
            action="auth.password_reset.request",
            target=username,
            result="failed",
            detail="该账号重置请求过于频繁，已跳过发送",
            ip=ip,
        )
        raise HTTPException(status_code=429, detail="提交过于频繁，请稍后再试")

    issued = await password_reset.issue(username)
    if not issued:
        await store.add_audit(
            username=username,
            action="auth.password_reset.request",
            target=username,
            result="failed",
            detail="账号不存在、未激活或没有可用邮箱",
            ip=ip,
        )
        return generic

    token, to_email = issued
    url = f"{await _panel_url(request)}/reset-password?token={token}"
    background.add_task(_deliver_password_reset, username, to_email, url, ip)
    return generic


@router.post("/reset-password/check")
async def check_reset_token(payload: ResetTokenIn) -> Dict[str, Any]:
    """打开重置页面时先确认链接还有效（不消耗令牌）。"""
    username = await password_reset.peek(payload.token or "")
    return {"valid": username is not None, "username": username or ""}


@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordIn, request: Request
) -> Dict[str, Any]:
    """自助重置第 2 步：凭令牌设置新密码。令牌一次性，用完即废。

    与登录 / 注册 / 忘记密码同级的人机校验：这一步会改掉账号口令并吊销该
    账号全部会话，是四个匿名入口里破坏力最大的一个。曾经漏掉了它（滑块
    模式下这个页面干脆没有验证码），补上时顺手把校验抽成 ``_check_captcha``
    共用，避免下一次再漏。

    顺序：口令强度 → 人机校验 → 兑换令牌。人机校验必须在 ``redeem`` **之前**
    —— 令牌是一次性的，验证码没过就把链接烧掉，用户得重新申请一封邮件。
    口令强度放最前是因为它只看输入本身，先拒掉能省一次验证码消耗。
    """
    ip = security.client_ip(request)

    weak = _password_weak_reason(payload.password)
    if weak:
        raise HTTPException(status_code=400, detail=weak)

    passed, detail, audit_note = await _check_captcha(
        captcha_id=payload.captcha_id,
        captcha_code=payload.captcha_code,
        captcha_x=payload.captcha_x,
        captcha_path=payload.captcha_path,
    )
    if not passed:
        # 还没兑换令牌，拿不到用户名；这条审计只能记来源 IP。
        await store.add_audit(
            username="",
            action="auth.password_reset",
            target="",
            result="failed",
            detail=f"{audit_note}，未重置密码",
            ip=ip,
        )
        raise HTTPException(status_code=400, detail=detail)

    username = await password_reset.redeem(payload.token or "")
    if not username:
        raise HTTPException(
            status_code=400, detail="重置链接无效或已过期，请重新申请"
        )

    updated = await store.update_user(username, password=payload.password)
    if not updated:
        raise HTTPException(status_code=404, detail="账号不存在")

    # 密码已经改了，该账号邮箱里可能还有几条旧链接，一并作废
    await password_reset.purge_user(username)

    # 重置密码往往是「账号可能已被盗」的应对手段：把已经登录的设备全部踢下线
    # 才有意义，否则攻击者手里的旧 token 还能继续用满 12 小时。API Token 也
    # 一并吊销 —— 它可能永不过期，漏掉它等于没做补救。
    revoked = await security.revoke_user_credentials(username)

    await store.add_audit(
        username=username,
        action="auth.password_reset",
        target=username,
        result="success",
        detail={
            "note": "自助重置密码成功",
            "sessions_revoked": revoked["sessions"],
            "api_tokens_revoked": revoked["api_tokens"],
        },
        ip=ip,
    )
    return {"ok": True, "username": username, "message": "密码已重置，请重新登录"}


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, str]:
    """真正的登出：撤销这次登录对应的**服务端会话**。

    只清前端 localStorage 是不够的 —— 那一枚 token 在服务端仍然有效，谁拿到
    （日志、代理、共享电脑）都还能继续用。这里撤销会话记录（sid），于是：

    * refresh token 再也换不出新令牌；
    * 这枚 access token 下一次请求就被拒（认证依赖会核对会话是否有效），
      不必等它自然过期，也不需要额外的黑名单表。
    """
    # 会话 ID 优先用认证时解析出来的那个（access token 里的 sid）：无论请求是
    # Cookie 还是 Authorization 来的，都能准确定位「本次登录」，不会出现
    # 「cookie 里没有 refresh 就撤不掉会话」的漏网。
    session = str(getattr(request.state, "session_id", "") or "")
    if not session:
        token = request.cookies.get(security.REFRESH_COOKIE, "")
        if token:
            try:
                payload = security.decode_token(token, expected_typ="refresh")
                if str(payload.get("sub") or "") == user["username"]:
                    session = str(payload.get("jti") or "")
            except HTTPException:
                # cookie 已过期/损坏：照样把它清掉，不算登出失败
                session = ""

    if session:
        await store.revoke_refresh_token(session)

    # 三枚 cookie 一起清：access 不清的话浏览器还会继续带着它，直到过期
    security.clear_session_cookies(response)
    await security.audit(request, user, "auth.logout", detail={"session": session})
    return {"message": "已退出登录"}


@router.post("/logout-all")
async def logout_all(
    request: Request,
    response: Response,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """退出所有设备：撤销全部会话、吊销全部 API Token、清掉信任设备，并把会话版本 +1。

    会话版本一变，所有已经发出去的 access token（包括当前这枚）立刻失效，
    所以前端拿到这个响应后应当回到登录页。

    这里连 API Token 一起吊销：用户点这个按钮的心智是「把我在外面的东西全
    停掉」（丢了电脑、怀疑泄漏），留一类长期凭据活着不符合预期。令牌是自助
    管理的，需要时重新签发即可。

    信任设备同理必须一起清：它同样是一份「不需要再输动态码」的长期凭证，
    而且是丢电脑时最先该担心那一类 —— 留着它，用户以为已经把那台机器赶走了，
    实际上对方 7 天内还能只凭口令进来。
    """
    revoked = await security.revoke_user_credentials(user["username"])
    security.clear_session_cookies(response)
    await security.audit(
        request,
        user,
        "auth.logout_all",
        detail={
            "sessions_revoked": revoked["sessions"],
            "api_tokens_revoked": revoked["api_tokens"],
        },
    )
    return {
        "message": "已退出所有设备",
        "sessions_revoked": revoked["sessions"],
        "api_tokens_revoked": revoked["api_tokens"],
        "token_version": revoked["token_version"],
    }


@router.get("/sessions")
async def list_sessions(
    request: Request,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """当前账号仍然有效的登录设备（个人中心展示 + 逐个踢下线）。"""
    rows = await store.list_refresh_tokens(user["username"])
    current = ""
    token = request.cookies.get(security.REFRESH_COOKIE, "")
    if token:
        try:
            current = str(
                security.decode_token(token, expected_typ="refresh").get("jti") or ""
            )
        except HTTPException:
            current = ""
    return {
        "sessions": [
            {
                "id": r.get("jti"),
                "created": r.get("created"),
                "expires_at": r.get("expires_at"),
                "ip": r.get("ip", ""),
                "user_agent": r.get("user_agent", ""),
                "current": r.get("jti") == current,
            }
            for r in rows
        ],
        # 上限一并返回：个人中心的「登录设备」据此说明「最多保留几台」，
        # 并引导用户怀疑泄露时用一键下线（BUG-019）。
        "limit": max(0, int(settings.session_max_per_user)),
    }


@router.delete("/sessions/{jti}")
async def revoke_session(
    jti: str,
    request: Request,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """踢掉自己的一台设备（只允许操作自己的会话）。"""
    record = await store.get_refresh_token(jti)
    if not record or record.get("username") != user["username"]:
        raise HTTPException(status_code=404, detail="会话不存在或已失效")
    await store.revoke_refresh_token(jti)
    await security.audit(request, user, "auth.session_revoke", target=jti)
    return {"ok": True, "message": "该设备已被退出登录"}


# ------------------------------------------------------------ 两步验证（TOTP）
@router.get("/2fa")
async def totp_status(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """两步验证状态：是否开启、是否被强制、还剩几张恢复码。"""
    record = await store.get_user(user["username"]) or {}
    return {
        "enabled": bool(record.get("totp_enabled")),
        "required": str(user.get("role") or "") in settings.totp_required_role_list,
        "recovery_codes_left": totp.remaining_recovery_codes(
            str(record.get("totp_recovery") or "")
        ),
    }


@router.post("/2fa/setup")
async def totp_setup(
    request: Request,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """生成一枚新种子（此时**还没启用**，要再用动态码确认一次）。

    种子用 crypto 加密后落库；返回里的明文只走这一次响应，前端扫完码就丢。
    """
    record = await store.get_user(user["username"])
    if not record:
        raise HTTPException(status_code=404, detail="用户不存在")
    if record.get("totp_enabled"):
        raise HTTPException(
            status_code=400, detail="两步验证已开启；如需更换认证器请先关闭再重新绑定"
        )

    secret = totp.generate_secret()
    await store.set_totp(
        user["username"], secret=crypto.encrypt(secret), enabled=False, recovery="[]"
    )
    uri = totp.provisioning_uri(secret, user["username"])
    await security.audit(request, user, "auth.totp_setup")
    return {
        "secret": secret,
        "otpauth_url": uri,
        "qr_png": totp.qr_png_data_uri(uri),
        "message": "请用认证器 App 扫码，然后输入当前动态码完成绑定",
    }


@router.post("/2fa/enable")
async def totp_enable(
    payload: TotpCodeIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """用一次动态码确认绑定成功，并下发一次性恢复码（只显示这一次）。"""
    record = await store.get_user(user["username"]) or {}
    if not record.get("totp_secret"):
        raise HTTPException(status_code=400, detail="请先生成两步验证密钥")
    if not totp.verify_code(str(record.get("totp_secret") or ""), payload.code):
        await security.audit(
            request, user, "auth.totp_enable", result="failed", detail="动态码不正确"
        )
        raise HTTPException(
            status_code=400,
            detail="动态码不正确，请确认手机时间与服务器同步后重试",
        )

    codes = totp.new_recovery_codes()
    await store.set_totp(
        user["username"], enabled=True, recovery=totp.hash_recovery_codes(codes)
    )
    # 绑定（含重新绑定）2FA 时把旧的信任设备整批作废。绑定是一次「换锁」：
    # 之前被信任的设备可能正是这次换锁的原因（手机丢了、换认证器），
    # 留着它们等于新锁配着旧钥匙。
    await trusteddevices.forget_all(user["username"])
    await security.audit(request, user, "auth.totp_enable")
    return {
        "ok": True,
        "recovery_codes": codes,
        "message": "两步验证已开启，请把恢复码保存在安全的地方（只显示这一次）",
    }


@router.post("/2fa/disable")
async def totp_disable(
    payload: TotpDisableIn,
    request: Request,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """关闭两步验证：口令 + 动态码双确认。

    只有一枚被盗的 access token 关不掉它，光知道密码也关不掉它 —— 否则
    「2FA 开启」在攻击者拿到 token 之后形同虚设。
    """
    record = await store.get_user(user["username"]) or {}
    if not record.get("totp_enabled"):
        raise HTTPException(status_code=400, detail="两步验证尚未开启")
    if str(user.get("role") or "") in settings.totp_required_role_list:
        raise HTTPException(
            status_code=403, detail="该角色被强制要求开启两步验证，无法关闭"
        )
    if not security.verify_password(
        payload.password, str(record.get("password_hash") or "")
    ):
        await security.audit(
            request, user, "auth.totp_disable", result="failed", detail="口令不正确"
        )
        raise HTTPException(status_code=400, detail="当前密码不正确")
    if not payload.code:
        raise HTTPException(status_code=400, detail="请输入当前动态码或一张恢复码")

    ok, _ = await totp.verify_login_code(record, payload.code)
    if not ok:
        await security.audit(
            request, user, "auth.totp_disable", result="failed", detail="动态码不正确"
        )
        raise HTTPException(status_code=400, detail="动态码不正确")

    await store.set_totp(user["username"], secret="", enabled=False, recovery="[]")
    # 关闭 2FA 同样清掉全部信任设备。信任记录的含义是「第二因子已在这台设备上
    # 通过」，2FA 一关这个前提就不成立了；更关键的是**将来重新开启时**旧记录
    # 会立刻复活成一条可绕过的通道 —— 那是「关了又开」最容易被漏掉的一个洞。
    await trusteddevices.forget_all(user["username"])
    await security.audit(request, user, "auth.totp_disable")
    return {"ok": True, "message": "两步验证已关闭"}


@router.post("/step-up")
async def step_up(
    payload: StepUpRequest,
    request: Request,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """敏感操作（删虚拟机、改连接凭据、增删账号…）前的二次确认。

    只验证密码还不够保险（密码可能和 token 一起泄露），开了两步验证的账号
    这里同样要求动态码或一张恢复码。通过后在**当前会话**上盖一个短时效的章
    （refresh_tokens.elevated_until），窗口内重复做敏感操作不必反复输入；
    换设备 / 换会话都要重新确认。
    """
    session_id = str(getattr(request.state, "session_id", "") or "")
    if not session_id:
        # 旧版本签发的令牌没有会话记录，无处盖章：请重新登录拿一份新会话
        raise HTTPException(status_code=401, detail="登录状态已失效，请重新登录")

    ip = security.client_ip(request)
    if await _anon_throttled("step-up", ip, 20):
        raise HTTPException(status_code=429, detail="提交过于频繁，请稍后再试")

    record = await store.get_user(user["username"]) or {}
    if not security.verify_password(
        payload.password, str(record.get("password_hash") or "")
    ):
        await _login_count_failure(user["username"], ip, "二次确认密码错误")
        await security.audit(
            request, user, "auth.step_up", result="failed", detail="密码不正确"
        )
        raise HTTPException(status_code=400, detail="密码不正确")

    if record.get("totp_enabled"):
        code = (payload.totp_code or "").strip()
        if not code:
            raise HTTPException(status_code=400, detail="请输入两步验证动态码")
        ok, _ = await totp.verify_login_code(record, code)
        if not ok:
            await _login_count_failure(user["username"], ip, "二次确认动态码错误")
            await security.audit(
                request, user, "auth.step_up", result="failed", detail="动态码不正确"
            )
            raise HTTPException(status_code=400, detail="动态码不正确")

    window = int(settings.step_up_window_minutes)
    until = await store.mark_session_elevated(session_id, window * 60)
    await security.audit(
        request, user, "auth.step_up", target=user["username"], detail={"minutes": window}
    )
    return {
        "ok": True,
        "expires_in": window * 60,
        "expires_at": until,
        "message": f"已确认身份，{window} 分钟内无需重复验证",
    }


@router.get("/me", response_model=UserOut)
async def me(user: Dict[str, Any] = Depends(security.get_current_user)) -> UserOut:
    return UserOut(
        id=user.get("id"),
        username=user["username"],
        role=user["role"],
        email=user.get("email", ""),
        enabled=user.get("enabled", True),
        permissions=user.get("permissions", []),
        totp_enabled=bool(user.get("totp_enabled")),
        totp_setup_required=bool(user.get("totp_setup_required")),
        expires_at=security.account_expires_at(user),
        # 区域管理员的可管理区域：前端据此决定建机向导里能选哪些区域
        regions=security.user_regions(user),
    )


@router.put("/me", response_model=UserOut)
async def update_me(
    payload: ProfileUpdate,
    request: Request,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> UserOut:
    """更新当前登录用户自己的个人信息。

    只允许改邮箱：用户名是登录凭据、角色与启用状态属于管理员职责，
    这里一律以 token 中的身份为准，不接受请求体里的任何提权字段。
    """
    email = (payload.email or "").strip()
    if len(email) > 255:
        raise HTTPException(status_code=400, detail="邮箱长度不能超过 255 个字符")
    if email and "@" not in email:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")

    record = await store.get_user(user["username"])
    if not record:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 邮箱是「找回密码」的收件地址：改它等于把账号的找回入口交出去。所以真正
    # 变更时必须再验证一次身份 —— 只凭一枚可能被捡到的 token 不算数。
    # 与「改密码 / 关两步验证」同档：当前口令（+ 开了 2FA 时的动态码）。
    changed = (record.get("email") or "").strip().lower() != email.lower()
    if changed:
        if not security.verify_password(
            payload.current_password or "", record["password_hash"]
        ):
            await security.audit(
                request,
                user,
                "auth.profile_update",
                target=user["username"],
                result="failed",
                detail="修改邮箱时当前密码不正确",
            )
            raise HTTPException(
                status_code=400, detail="修改邮箱需要输入当前密码"
            )
        if record.get("totp_enabled"):
            if not payload.totp_code:
                raise HTTPException(status_code=400, detail="请输入两步验证动态码")
            ok, _ = await totp.verify_login_code(record, payload.totp_code)
            if not ok:
                await security.audit(
                    request,
                    user,
                    "auth.profile_update",
                    target=user["username"],
                    result="failed",
                    detail="修改邮箱时两步验证码不正确",
                )
                raise HTTPException(status_code=400, detail="两步验证码不正确")

    updated = await store.update_user(user["username"], email=email) or {}
    await security.audit(
        request,
        user,
        "auth.profile_update",
        target=user["username"],
        detail={"email": email},
    )
    return _user_out(updated)


@router.post("/password")
async def change_password(
    payload: PasswordChange,
    request: Request,
    response: Response,
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """修改当前登录用户自己的密码（需校验当前密码，开了 2FA 还要动态码）。

    改完密码会把**其它设备**全部踢下线（撤销会话 + 会话版本 +1），当前这次
    请求换一份新令牌继续用 —— 「密码可能泄露了」的第一反应就是改密码，那么
    旧 token 不能还留着。
    """
    record = await store.get_user(user["username"])
    if not record:
        raise HTTPException(status_code=404, detail="用户不存在")

    if not security.verify_password(payload.current_password, record["password_hash"]):
        await security.audit(
            request,
            user,
            "auth.password_change",
            target=user["username"],
            result="failed",
            detail="当前密码不正确",
        )
        raise HTTPException(status_code=400, detail="当前密码不正确")

    # 两步验证：改密码属于敏感操作，即使 token 被盗也不能单靠密码完成
    if record.get("totp_enabled"):
        if not payload.totp_code:
            raise HTTPException(status_code=400, detail="请输入两步验证动态码")
        ok, _ = await totp.verify_login_code(record, payload.totp_code)
        if not ok:
            await security.audit(
                request,
                user,
                "auth.password_change",
                target=user["username"],
                result="failed",
                detail="两步验证码不正确",
            )
            raise HTTPException(status_code=400, detail="两步验证码不正确")

    if payload.new_password == payload.current_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    weak = _password_weak_reason(payload.new_password)
    if weak:
        raise HTTPException(status_code=400, detail=weak)

    await store.update_user(user["username"], password=payload.new_password)
    # 旧凭据一律作废（含已签发的 API Token，它们可能永不过期）；本次请求重新
    # 签发一份会话，用户不必立刻重登。
    revoked = await security.revoke_user_credentials(user["username"])
    fresh = await store.get_user(user["username"]) or record
    access = await _issue_session(response, fresh, request)

    await security.audit(
        request,
        user,
        "auth.password_change",
        target=user["username"],
        detail={
            "sessions_revoked": revoked["sessions"],
            "api_tokens_revoked": revoked["api_tokens"],
        },
    )
    return {
        "ok": True,
        "message": "密码已更新，其它设备已退出登录",
        "access_token": access,
        "other_sessions_revoked": revoked["sessions"],
        "api_tokens_revoked": revoked["api_tokens"],
    }


@router.get("/my-permissions")
async def my_permissions(
    user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """当前用户自己的角色与生效权限（含中文名，供「个人中心」只读展示）。

    普通用户没有 users.view，拿不到 /api/roles 与 /api/permissions/catalog，
    所以这里由服务端把权限 key 翻译成可读标签，只返回自己的那一份。
    """
    admin = security.is_admin(user)
    granted = set(user.get("permissions") or [])
    role_id = str(user.get("role") or "")
    role = await store.get_role(role_id) or {}

    groups = []
    for group in i18n.localize_permission_catalog(security.PERMISSION_CATALOG):
        items = [
            {
                "key": item["key"],
                "label": item.get("label", item["key"]),
                "desc": item.get("desc", ""),
            }
            for item in group.get("permissions", [])
            if admin or item["key"] in granted
        ]
        if items:
            groups.append({"key": group["key"], "label": group["label"], "permissions": items})

    return {
        "role": role_id,
        "role_name": i18n.tr(role.get("name") or role_id),
        "role_description": i18n.tr(role.get("description") or ""),
        "is_admin": admin,
        "permissions": sorted(granted),
        "groups": groups,
    }


@router.get("/permissions")
async def permissions(
    _user: Dict[str, Any] = Depends(security.get_current_user),
) -> Dict[str, Any]:
    """角色/权限清单（需登录）。

    原来匿名可读：三种角色的完整权限边界一览无余，等于替攻击者标好了
    「拿到 operator 能做什么」——例如 vm.console / firewall.manage 这些
    横向操作的入口。登录后前端照常可用（本面板前端不依赖它，留给 API 客户端）。
    """
    return {
        "roles": [
            {
                "role": role,
                "description": security.ROLE_DESCRIPTIONS.get(role, ""),
                "permissions": perms,
            }
            for role, perms in security.ROLE_PERMISSIONS.items()
        ]
    }
