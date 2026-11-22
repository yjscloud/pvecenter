/* ==========================================================================
   pvecenter — 注册页
   自助注册默认待审批；免审批的邀请链接 / 注册码建号即 active。成功页必须
   以后端返回的 status 为准，不能自行按链接预览推断（注册码没有预览接口）。
   ========================================================================== */

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useSearchParams } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { authApi } from '../api/endpoints';
import { errorMessage } from '../api/client';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Notice } from '../components/ui/EmptyState';
import { SliderCaptcha } from '../components/SliderCaptcha';
import type { SliderTracePoint } from '../api/types';
import {
  IconAlert,
  IconCheck,
  IconEye,
  IconEyeOff,
  IconKey,
  IconShield,
  IconUser,
} from '../components/Icons';
import { AuthShell } from '../components/AuthShell';
import { useT } from '../i18n';

/* 与后端 users.py 的用户名规则保持一致 */
const USERNAME_RE = /^[a-zA-Z0-9._-]{3,32}$/;

export function Register() {
  const t = useT();
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [captchaCode, setCaptchaCode] = useState('');
  /* 字段级错误：置空用 undefined（清掉某一个字段的提示，而不是整表重建） */
  const [errors, setErrors] = useState<Record<string, string | undefined>>({});
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState(false);
  /* 注册接口返回的账号状态：active = 免审批、可直接登录；pending = 待审批。
     成功页只认这个值，避免注册码 / 已消费的邀请链接被误判成待审批。 */
  const [registrationStatus, setRegistrationStatus] = useState<string | null>(null);
  /* 邀请码 / 注册码：具体要哪一种由当前注册方式决定 */
  const [credential, setCredential] = useState('');
  /* 邮箱验证码：自助注册必填。发送与提交分成两步 —— 发信要有自己的冷却，
     和提交绑在一起就是个可以被脚本无限触发的发信接口。 */
  const [emailCode, setEmailCode] = useState('');
  const [codeSending, setCodeSending] = useState(false);
  /* 重发倒计时（秒）：发送成功后按后端给的 resend_after 开始走 */
  const [codeCooldown, setCodeCooldown] = useState(0);
  /* 已发送到的邮箱：用来在提示里写清「发到了哪个地址」，避免用户填错邮箱还在等 */
  const [codeSentTo, setCodeSentTo] = useState('');
  /* 人机校验是否已被「发送验证码」消耗掉。
     发码必须过一道人机校验，而那道题是一次性的：发完就作废。与其立刻换一张
     新题（看起来像「发个码把人机校验重置了」），不如把它收成一行「已通过」，
     等用户真要重发时再重新出题。 */
  const [captchaDone, setCaptchaDone] = useState(false);

  /*
    注册方式（设置页可切：关闭 / 邀请制 / 注册码制 / 自助）。需要凭证的模式多
    渲染一个输入框；关闭时整页替换成「已关闭」提示，表单与验证码挑战都不加载
    —— 不然填完一屏才在提交时拿到 403。接口失败按「开放」处理（老后端没有这
    个接口），行为与改造前一致。
  */
  const modeQ = useQuery({
    queryKey: ['registration-mode'],
    queryFn: authApi.registrationMode,
    refetchOnWindowFocus: false,
    retry: 0,
    staleTime: 5 * 60_000,
  });
  const registerClosed = modeQ.data?.mode === 'off';
  /* 邮箱验证码策略由后端给（有效期 / 可错次数 / 冷却）：这里只读，不自己算，
     否则改了环境变量前端会与后端说法不一致。required=false 时整块不渲染。 */
  const codePolicy = modeQ.data?.email_code;
  const codeRequired = Boolean(codePolicy?.required);

  /*
    邀请链接：管理员把 ``/register?invite=<token>`` 发出去，注册页带着它打开。
    链接自带角色与审批策略，它本身就是凭证 —— 所以带了链接就不再显示邀请码 /
    注册码输入框，也不再要求填。预览接口在提交前先问一次「还能不能用、给什么
    角色、要不要审批」，链接失效时表单直接禁用，省得填完一屏才被拒。
  */
  const [searchParams] = useSearchParams();
  const inviteToken = (searchParams.get('invite') ?? '').trim();
  const inviteQ = useQuery({
    queryKey: ['invite-preview', inviteToken],
    queryFn: () => authApi.invitePreview(inviteToken),
    enabled: Boolean(inviteToken) && !registerClosed,
    refetchOnWindowFocus: false,
    retry: 0,
    staleTime: 60_000,
  });
  const invite = inviteQ.data;
  const inviteInvalid = Boolean(inviteToken) && inviteQ.isSuccess && !invite?.valid;
  /* 链接只在邀请制下生效：注册方式不是邀请制时预览也会回 valid:false，
     这里单独区分文案，免得说成「链接过期了」误导人 */
  const inviteModeMismatch =
    Boolean(inviteToken) && modeQ.isSuccess && modeQ.data?.mode !== 'invite';
  /* 提交前用链接预览预测措辞；提交后必须以注册接口返回的 status 为准。
     注册码没有公开预览接口，不能靠 invite?.requires_approval 推断。 */
  const activeAfterRegister = done
    ? registrationStatus === 'active'
    : Boolean(invite?.valid && invite.requires_approval === false);
  /* '' | 'invite' | 'code'：要填哪种凭证（带链接时不需要） */
  const needsCredential = inviteToken ? '' : (modeQ.data?.requires || '');
  /* 邀请制只认邀请链接：直接打开注册页（没有 ?invite=）时无从校验，禁用提交
     并提示去要链接，而不是让人填完一屏才被拒 */
  const inviteNeedsLink = modeQ.isSuccess && modeQ.data?.mode === 'invite' && !inviteToken;

  /*
    人机校验：与登录共用同一套「设置 → 登录验证」配置（关闭 / 图形码 / 滑块）。
    单独一个 queryKey —— 挑战是一次性的，和登录页共用缓存会互相把对方的题作废。
    注册已关闭时不用签发挑战：那一趟 CPU 白花。
  */
  const captchaQ = useQuery({
    queryKey: ['register-captcha'],
    queryFn: authApi.captcha,
    enabled: !registerClosed,
    refetchOnWindowFocus: false,
    staleTime: 0,
    retry: 1,
  });
  const captchaMode =
    captchaQ.data?.mode ?? (captchaQ.data?.required ? 'image' : 'off');
  const captchaRequired = captchaMode !== 'off';

  /* 滑块位置只在提交时读一次，拖动过程中不触发整表单重渲染（同登录页） */
  const sliderXRef = useRef(0);
  /* 拖动轨迹：只交坐标不交轨迹会被服务端判失败（安全审计 H-1） */
  const sliderPathRef = useRef<SliderTracePoint[]>([]);
  const handleSliderPosition = useCallback((x: number, path: SliderTracePoint[]) => {
    sliderXRef.current = x;
    sliderPathRef.current = path;
    if (x > 0) {
      setErrors((prev) => (prev.captcha ? { ...prev, captcha: undefined } : prev));
    }
  }, []);

  const validate = (): boolean => {
    const next: Record<string, string | undefined> = {};
    const name = username.trim();

    if (!name) {
      next.username = t('register.err.usernameRequired');
    } else if (!USERNAME_RE.test(name)) {
      next.username = t('register.err.usernameRule');
    }

    // 邮箱必填：审批结果（通过 / 拒绝）会发到这里
    if (!email.trim()) {
      next.email = t('register.err.emailRequired');
    } else if (!email.includes('@')) {
      next.email = t('register.err.emailInvalid');
    }

    if (!password) {
      next.password = t('register.err.passwordRequired');
    } else if (password.length < 12) {
      next.password = t('register.err.passwordTooShort');
    } else if (!/[a-zA-Z]/.test(password) || !/\d/.test(password)) {
      next.password = t('register.err.passwordWeak');
    }

    if (!confirm) {
      next.confirm = t('register.err.confirmRequired');
    } else if (confirm !== password) {
      next.confirm = t('register.err.confirmMismatch');
    }

    if (needsCredential && !credential.trim()) {
      next.credential =
        needsCredential === 'invite'
          ? t('register.err.inviteRequired')
          : t('register.err.codeRequired');
    }

    if (codeRequired) {
      /* 自助注册：验证码就是提交时的凭证，本地先拦「没填 / 位数不对」 */
      const code = emailCode.trim();
      if (!code) {
        next.emailCode = t('register.err.emailCodeRequired');
      } else if (!/^\d{6}$/.test(code)) {
        next.emailCode = t('register.err.emailCodeInvalid');
      }
    } else if (captchaMode === 'image' && !captchaCode.trim()) {
      next.captcha = t('auth.err.captchaRequired');
    } else if (captchaMode === 'slider') {
      /* 目标位置只在轨道图里，本地只能拦「压根没拖」，对齐与否交给服务端 */
      if (sliderXRef.current <= 0) {
        next.captcha = t('auth.err.sliderRequired');
      }
    }

    setErrors(next);
    return Object.keys(next).length === 0;
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting || inviteInvalid || inviteNeedsLink) return;
    if (inviteToken && inviteQ.isLoading) return;
    setError(null);
    if (!validate()) return;

    setSubmitting(true);
    setRegistrationStatus(null);
    try {
      const res = await authApi.register({
        username: username.trim(),
        password,
        email: email.trim(),
        /* 有邮箱验证码时后端不再要人机校验（发码那一步已经过了一道题，
           挑战也被消耗了），这里不要塞一个已失效的 captcha_id 过去 */
        captcha_id:
          !codeRequired && captchaRequired ? (captchaQ.data?.id ?? '') : undefined,
        captcha_code:
          !codeRequired && captchaMode === 'image' ? captchaCode.trim() : undefined,
        captcha_x:
          !codeRequired && captchaMode === 'slider' ? sliderXRef.current : undefined,
        captcha_path:
          !codeRequired && captchaMode === 'slider' ? sliderPathRef.current : undefined,
        email_code: codeRequired ? emailCode.trim() : undefined,
        invite_code: needsCredential ? credential.trim() : undefined,
        invite_token: inviteToken || undefined,
      });
      setRegistrationStatus(res.status);
      setDone(true);
    } catch (err) {
      const msg = errorMessage(err);
      /* 邮箱验证码的错误（填错 / 过期 / 次数用尽）归位到验证码字段；
         其余图形码类失败仍按登录页的老规矩处理 */
      if (/邮箱验证码/.test(msg)) {
        setErrors({ emailCode: msg });
      } else if (!codeRequired && captchaRequired && /验证码|滑块/.test(msg)) {
        setErrors({ captcha: msg });
      } else {
        setError(msg);
      }
      /* 挑战一次性：失败后换一张，顺带清掉上次作答。走邮箱验证码时那道
         挑战早在发信时就被消耗掉了，这里再换一张没有意义。 */
      if (!codeRequired && captchaRequired) {
        setCaptchaCode('');
        sliderXRef.current = 0;
        void captchaQ.refetch();
      }
    } finally {
      setSubmitting(false);
    }
  };

  /* 重发倒计时：发送成功后按后端给的 resend_after 每秒减一。
     放在 setTimeout 里逐次触发，而不是 setInterval —— 卸载时只需清一个句柄。 */
  useEffect(() => {
    if (codeCooldown <= 0) return undefined;
    const timer = window.setTimeout(
      () => setCodeCooldown((n) => Math.max(0, n - 1)),
      1000,
    );
    return () => window.clearTimeout(timer);
  }, [codeCooldown]);

  /* 重新亮出人机校验：换一张新挑战，清掉上一次的作答。
     发码成功后、以及用户主动点「重新验证」时都走这里。 */
  const resetHumanCheck = () => {
    setCaptchaDone(false);
    setCaptchaCode('');
    sliderXRef.current = 0;
    setErrors((prev) => ({ ...prev, captcha: undefined }));
    void captchaQ.refetch();
  };

  /* 发码：与提交分开的第二次「对外动作」。
     人机校验在这一步消耗（发信必须有人机，否则就是个免费邮件轰炸接口），
     提交时只认验证码本身 —— 所以提交那边不再要求图形码。 */
  const handleSendCode = async () => {
    if (codeSending || submitting) return;
    /* 上一次发码已经把挑战用掉了：先重新出题，解完再点一次发送。
       直接发出去的话服务端只会回「验证码已失效」，用户还得自己猜为什么。 */
    if (captchaRequired && captchaDone) {
      resetHumanCheck();
      return;
    }
    const mail = email.trim();

    const next: Record<string, string | undefined> = {};
    if (!mail) next.email = t('register.err.emailRequired');
    else if (!mail.includes('@')) next.email = t('register.err.emailInvalid');
    if (captchaMode === 'image' && !captchaCode.trim()) {
      next.captcha = t('auth.err.captchaRequired');
    } else if (captchaMode === 'slider' && sliderXRef.current <= 0) {
      next.captcha = t('auth.err.sliderRequired');
    }
    if (Object.keys(next).length > 0) {
      setErrors(next);
      return;
    }

    setError(null);
    setCodeSending(true);
    try {
      const res = await authApi.sendRegisterEmailCode({
        email: mail,
        captcha_id: captchaRequired ? (captchaQ.data?.id ?? '') : undefined,
        captcha_code: captchaMode === 'image' ? captchaCode.trim() : undefined,
        captcha_x: captchaMode === 'slider' ? sliderXRef.current : undefined,
        captcha_path: captchaMode === 'slider' ? sliderPathRef.current : undefined,
      });
      setCodeSentTo(mail);
      setCodeCooldown(res.resend_after);
      /* 这一次人机校验已经被服务端消耗：把挑战区收成「已通过」，
         不要立刻换一张新题 —— 用户会以为发码失败了、或者要再解一遍。 */
      setCaptchaDone(captchaRequired);
      setErrors((prev) => ({
        ...prev,
        email: undefined,
        captcha: undefined,
        emailCode: undefined,
      }));
    } catch (err) {
      const msg = errorMessage(err);
      setError(msg);
      /* 限频时按后端说的秒数继续倒计时，否则按钮一直是可点的，点一次一个 429 */
      const wait = /(\d+)\s*秒/.exec(msg);
      if (wait) setCodeCooldown(Number(wait[1]));
      /* 失败时挑战多半已经作废（尤其是「验证码不正确」），换一张让用户能重试 */
      if (captchaRequired) {
        setCaptchaCode('');
        sliderXRef.current = 0;
        void captchaQ.refetch();
      }
    } finally {
      setCodeSending(false);
    }
  };

  /* 关闭态提前返回：所有 hook 都已在上面调用完，条件切换不影响 hook 顺序 */
  if (registerClosed) {
    return (
      <AuthShell>
        <h1 className="login-title">{t('register.closedTitle')}</h1>
        <p className="login-subtitle">{t('register.closedBody')}</p>
        <div className="login-form">
          <Link to="/login" style={{ textDecoration: 'none' }}>
            <Button variant="primary" size="lg" block>
              {t('auth.backToLogin')}
            </Button>
          </Link>
        </div>
      </AuthShell>
    );
  }

  return (
    <AuthShell>
      <h1 className="login-title">
        {done
          ? activeAfterRegister
            ? t('register.successActiveTitle')
            : t('register.titleDone')
          : t('register.title')}
      </h1>
      <p className="login-subtitle">
        {done
          ? activeAfterRegister
            ? t('register.subtitleActiveDone')
            : t('register.subtitleDone')
          : activeAfterRegister
            ? t('register.subtitleActive')
            : t('register.subtitle')}
      </p>

      {done ? (
        <>
          <div className="mb-16">
            {activeAfterRegister ? (
              <Notice tone="success" title={t('register.successActiveTitle')}>
                <b className="mono">{username.trim()}</b>
                {t('register.successActiveBody')}
              </Notice>
            ) : (
              <Notice tone="success" title={t('register.successTitle')}>
                {t('register.successBodyPre')}
                <b className="mono">{username.trim()}</b>
                {t('register.successBodyPost')}
              </Notice>
            )}
          </div>

          <div className="login-form">
            <Link to="/login" style={{ textDecoration: 'none' }}>
              <Button variant="primary" size="lg" block>
                {t('auth.backToLogin')}
              </Button>
            </Link>
          </div>
        </>
      ) : (
        <form className="login-form" onSubmit={handleSubmit} noValidate>
          {/* 邀请链接状态：能用的先说清「你将以什么角色加入、要不要等审批」，
              不能用的直接把表单拦在上面，别等填完再报错 */}
          {inviteToken ? (
            inviteQ.isLoading ? (
              <div className="fs-sm text-muted">{t('register.inviteLinkChecking')}</div>
            ) : invite?.valid ? (
              <Notice tone="info" title={t('register.inviteLinkTitle')}>
                {t('register.inviteLinkBody', {
                  role: invite.role_name || invite.role || '',
                  approval: invite.requires_approval
                    ? t('register.inviteLinkApproval')
                    : t('register.inviteLinkNoApproval'),
                })}
                {invite.remaining === null || invite.remaining === undefined
                  ? ` · ${t('register.inviteLinkUnlimited')}`
                  : ` · ${t('register.inviteLinkRemaining', { n: invite.remaining })}`}
              </Notice>
            ) : (
              // 不直接显示后端的 reason：那是中文写死的，英文界面会混一行中文。
              // 无效的三种原因（失效 / 过期 / 次数用尽）在 i18n 文案里一并说清。
              <Notice
                tone="danger"
                title={
                  inviteModeMismatch
                    ? t('register.inviteLinkModeTitle')
                    : t('register.inviteLinkInvalidTitle')
                }
              >
                {inviteModeMismatch
                  ? t('register.inviteLinkModeBody')
                  : t('register.inviteLinkInvalidBody')}
              </Notice>
            )
          ) : null}

          {inviteNeedsLink ? (
            <Notice tone="warning" title={t('register.inviteNeedsLinkTitle')}>
              {t('register.inviteNeedsLinkBody')}
            </Notice>
          ) : null}

          {error ? (
            <div className="login-error" role="alert">
              <IconAlert size={16} />
              <span>{error}</span>
            </div>
          ) : null}

          <Input
            label={t('register.username')}
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder={t('register.usernamePlaceholder')}
            autoComplete="username"
            autoFocus
            disabled={submitting}
            prefix={<IconUser size={15} />}
            error={errors.username}
            hint={t('register.usernameHint')}
            required
          />

          <Input
            label={t('register.password')}
            type={showPassword ? 'text' : 'password'}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder={t('register.passwordPlaceholder')}
            autoComplete="new-password"
            disabled={submitting}
            prefix={<IconKey size={15} />}
            error={errors.password}
            required
            suffix={
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={
                  showPassword ? t('auth.login.hidePassword') : t('auth.login.showPassword')
                }
                title={
                  showPassword ? t('auth.login.hidePassword') : t('auth.login.showPassword')
                }
                style={{
                  background: 'none',
                  border: 'none',
                  color: 'var(--text-muted)',
                  cursor: 'pointer',
                  display: 'inline-flex',
                  padding: 0,
                }}
              >
                {showPassword ? <IconEyeOff size={15} /> : <IconEye size={15} />}
              </button>
            }
          />

          <Input
            label={t('register.confirmPassword')}
            type={showPassword ? 'text' : 'password'}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            placeholder={t('register.confirmPlaceholder')}
            autoComplete="new-password"
            disabled={submitting}
            prefix={<IconCheck size={15} />}
            error={errors.confirm}
            required
          />

          {/* 邮箱排在确认密码之后：先把「这个账号是什么」填完（用户名 + 两次
              口令），再填与账号绑定的联系方式。之前夹在用户名与密码之间，把
              口令那两步切成了两段。 */}
          <Input
            label={t('register.email')}
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder={t('register.emailPlaceholder')}
            autoComplete="email"
            disabled={submitting}
            error={errors.email}
            hint={t('register.emailHint')}
            required
          />

          {/* 邀请制 / 注册码制：输入框与提示随注册方式切换（设置页可改） */}
          {needsCredential ? (
            <Input
              label={
                needsCredential === 'invite'
                  ? t('register.inviteCode')
                  : t('register.regCode')
              }
              value={credential}
              onChange={(e) => setCredential(e.target.value)}
              placeholder={
                needsCredential === 'invite'
                  ? t('register.inviteCodePlaceholder')
                  : t('register.regCodePlaceholder')
              }
              autoComplete="off"
              disabled={submitting}
              prefix={<IconKey size={15} />}
              error={errors.credential}
              hint={
                needsCredential === 'invite'
                  ? t('register.inviteCodeHint')
                  : t('register.regCodeHint')
              }
              required
            />
          ) : null}

          {/* 人机校验：文案复用登录页那几条（同为「验证码 / 拖动滑块」）。
              发码成功后人机校验已被消耗，这里换成一行「已通过」—— 立刻重新
              出题会让人以为发码失败、或者要再解一遍。 */}
          {captchaRequired && captchaDone ? (
            <div className="login-captcha-done">
              <IconCheck size={15} />
              <span>{t('register.humanCheckDone')}</span>
              <button
                type="button"
                className="login-code-btn"
                onClick={resetHumanCheck}
                disabled={submitting}
              >
                {t('register.humanCheckReset')}
              </button>
            </div>
          ) : null}

          {!captchaDone && captchaMode === 'image' ? (
            <div className="login-captcha">
              <div className="login-captcha-input">
                <Input
                  /* 自助注册时这道题是为「发送验证码」解的，叫「验证码」会与
                     上面的邮箱验证码撞名，用户会以为要填两遍同一个东西 */
                  label={codeRequired ? t('register.humanCheck') : t('auth.login.captcha')}
                  value={captchaCode}
                  onChange={(e) => setCaptchaCode(e.target.value)}
                  placeholder={t('auth.login.captchaPlaceholder')}
                  autoComplete="off"
                  autoCapitalize="characters"
                  disabled={submitting}
                  maxLength={8}
                  prefix={<IconShield size={15} />}
                  error={errors.captcha}
                  required
                />
              </div>
              <button
                type="button"
                className="login-captcha-img"
                onClick={() => {
                  setCaptchaCode('');
                  setErrors((prev) => ({ ...prev, captcha: undefined }));
                  void captchaQ.refetch();
                }}
                title={t('auth.login.captchaRefresh')}
                aria-label={t('auth.login.captchaRefreshAria')}
              >
                {captchaQ.data?.image ? (
                  <img src={captchaQ.data.image} alt={t('auth.login.captchaAlt')} />
                ) : (
                  <span>
                    {captchaQ.isFetching
                      ? t('auth.login.captchaLoading')
                      : t('auth.login.captchaFailed')}
                  </span>
                )}
              </button>
            </div>
          ) : !captchaDone && captchaMode === 'slider' ? (
            <SliderCaptcha
              challenge={captchaQ.data ?? { required: true, mode: 'slider' }}
              onPosition={handleSliderPosition}
              onRefresh={() => {
                sliderXRef.current = 0;
                setErrors((prev) => ({ ...prev, captcha: undefined }));
                void captchaQ.refetch();
              }}
              loading={captchaQ.isFetching}
              disabled={submitting}
              error={errors.captcha}
            />
          ) : null}

          {/* 自助注册：邮箱验证码。放在人机校验之后 —— 发信要先过那道题，
              顺序反过来的话用户得先滚下去解题、再滚回来点「发送」。
              发送按钮挂在输入框的 suffix 里，不额外占一行。 */}
          {codeRequired ? (
            <Input
              label={t('register.emailCode')}
              value={emailCode}
              onChange={(e) => {
                /* 只收数字：邮件里是 6 位数字，粘贴时常见的空格一并去掉 */
                setEmailCode(e.target.value.replace(/\D/g, '').slice(0, 6));
                if (errors.emailCode) {
                  setErrors((prev) => ({ ...prev, emailCode: undefined }));
                }
              }}
              placeholder={t('register.emailCodePlaceholder')}
              autoComplete="one-time-code"
              inputMode="numeric"
              disabled={submitting}
              prefix={<IconShield size={15} />}
              error={errors.emailCode}
              hint={
                codeSentTo
                  ? t('register.emailCodeSent', { email: codeSentTo })
                  : t('register.emailCodeHint', {
                      minutes: Math.max(
                        1,
                        Math.round((codePolicy?.ttl_seconds ?? 600) / 60),
                      ),
                      attempts: codePolicy?.max_attempts ?? 5,
                    })
              }
              required
              suffix={
                <button
                  type="button"
                  className="login-code-btn"
                  onClick={handleSendCode}
                  disabled={codeSending || codeCooldown > 0 || submitting}
                  title={t('register.emailCodeSend')}
                >
                  {codeSending
                    ? t('register.emailCodeSending')
                    : codeCooldown > 0
                      ? t('register.emailCodeResendIn', { seconds: codeCooldown })
                      : codeSentTo
                        ? t('register.emailCodeResend')
                        : t('register.emailCodeSend')}
                </button>
              }
            />
          ) : null}

          <Button
            type="submit"
            variant="primary"
            size="lg"
            block
            loading={submitting}
            /* 链接失效 / 还在校验 / 邀请制却没有链接时不放行：提交必然被拒 */
            disabled={
              inviteInvalid ||
              inviteNeedsLink ||
              (Boolean(inviteToken) && inviteQ.isLoading)
            }
          >
            {submitting ? t('register.submitting') : t('register.submit')}
          </Button>
        </form>
      )}

      <div className="login-health">
        <div className="login-health-row">
          <span className="login-health-label">{t('register.haveAccount')}</span>
          <Link to="/login" className="fs-sm">
            {t('auth.backToLogin')}
          </Link>
        </div>
      </div>
    </AuthShell>
  );
}
