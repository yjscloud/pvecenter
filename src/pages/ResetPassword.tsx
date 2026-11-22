/* ==========================================================================
   pvecenter — 设置新密码（自助重置第 2 步）
   从邮件链接进来（/reset-password?token=...），校验令牌后设置新密码。

   令牌校验在打开页面时就做：链接失效了就直接说明原因，
   别让人填完密码才发现提交不上去。
   ========================================================================== */

import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router';
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
} from '../components/Icons';
import { AuthShell } from '../components/AuthShell';
import { useT } from '../i18n';

/* 三个状态：还没查到结果 / 链接可用 / 链接不可用 */
type TokenState = 'checking' | 'valid' | 'invalid';

export function ResetPassword() {
  const t = useT();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const token = (searchParams.get('token') || '').trim();

  const [state, setState] = useState<TokenState>('checking');
  const [account, setAccount] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string | undefined>>({});
  const [submitting, setSubmitting] = useState(false);

  /*
    人机校验：与登录 / 注册 / 忘记密码共用同一套「设置 → 登录验证」配置。
    这一步会改掉账号口令并吊销全部会话，是四个匿名入口里破坏力最大的一个，
    所以必须和登录同级 —— 早先这里什么都没校验，滑块模式下页面也看不到滑块。

    queryKey 单独一份：挑战一次性，和别的页面共用缓存会互相把对方的题作废。
  */
  const captchaQ = useQuery({
    queryKey: ['reset-captcha'],
    queryFn: authApi.captcha,
    refetchOnWindowFocus: false,
    staleTime: 0,
    retry: 1,
  });
  const captchaMode =
    captchaQ.data?.mode ?? (captchaQ.data?.required ? 'image' : 'off');
  const captchaRequired = captchaMode !== 'off';

  const [captchaCode, setCaptchaCode] = useState('');
  const sliderXRef = useRef(0);
  const sliderPathRef = useRef<SliderTracePoint[]>([]);
  const handleSliderPosition = useCallback((x: number, path: SliderTracePoint[]) => {
    sliderXRef.current = x;
    sliderPathRef.current = path;
    if (x > 0) {
      setErrors((prev) => (prev.captcha ? { ...prev, captcha: undefined } : prev));
    }
  }, []);

  useEffect(() => {
    if (!token) {
      setState('invalid');
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const res = await authApi.checkResetToken(token);
        if (cancelled) return;
        setState(res.valid ? 'valid' : 'invalid');
        setAccount(res.username);
      } catch {
        if (!cancelled) setState('invalid');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const validate = (): boolean => {
    const next: Record<string, string | undefined> = {};
    if (!password) {
      next.password = t('reset.err.passwordRequired');
    } else if (password.length < 12) {
      next.password = t('reset.err.passwordTooShort');
    } else if (!/[a-zA-Z]/.test(password) || !/\d/.test(password)) {
      next.password = t('reset.err.passwordWeak');
    }
    if (!confirm) {
      next.confirm = t('reset.err.confirmRequired');
    } else if (confirm !== password) {
      next.confirm = t('reset.err.confirmMismatch');
    }
    if (captchaMode === 'image' && !captchaCode.trim()) {
      next.captcha = t('auth.err.captchaRequired');
    } else if (captchaMode === 'slider' && sliderXRef.current <= 0) {
      next.captcha = t('auth.err.sliderRequired');
    }
    setErrors(next);
    return Object.keys(next).length === 0;
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting) return;
    setError(null);
    if (!validate()) return;

    setSubmitting(true);
    try {
      await authApi.resetPassword({
        token,
        password,
        captcha_id: captchaRequired ? (captchaQ.data?.id ?? '') : undefined,
        captcha_code: captchaMode === 'image' ? captchaCode.trim() : undefined,
        captcha_x: captchaMode === 'slider' ? sliderXRef.current : undefined,
        captcha_path: captchaMode === 'slider' ? sliderPathRef.current : undefined,
      });
      // 成功后回登录页：令牌已作废，留在本页没有意义
      navigate('/login', { replace: true });
    } catch (err) {
      const msg = errorMessage(err);
      if (captchaRequired && /验证码|滑块/.test(msg)) {
        setErrors({ captcha: msg });
        // 挑战一次性：失败后换一道，顺带清掉上次作答
        setCaptchaCode('');
        sliderXRef.current = 0;
        void captchaQ.refetch();
      } else {
        setError(msg);
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AuthShell>
          {state === 'checking' ? (
            <>
              <h1 className="login-title">{t('reset.titleChecking')}</h1>
              <p className="login-subtitle">{t('reset.subtitleChecking')}</p>
            </>
          ) : null}

          {state === 'invalid' ? (
            <>
              <h1 className="login-title">{t('reset.titleInvalid')}</h1>
              <p className="login-subtitle">{t('reset.subtitleInvalid')}</p>

              <div className="mb-16">
                <Notice tone="warning" title={t('reset.invalidNoticeTitle')}>
                  {t('reset.invalidNoticeBody')}
                </Notice>
              </div>

              <div className="login-form">
                <Link to="/forgot-password" style={{ textDecoration: 'none' }}>
                  <Button variant="primary" size="lg" block>
                    {t('reset.reapply')}
                  </Button>
                </Link>
              </div>

              <div className="login-health">
                <div className="login-health-row">
                  <span className="login-health-label">{t('forgot.remembered')}</span>
                  <Link to="/login" className="fs-sm">
                    {t('auth.backToLogin')}
                  </Link>
                </div>
              </div>
            </>
          ) : null}

          {state === 'valid' ? (
            <>
              <h1 className="login-title">{t('reset.titleValid')}</h1>
              <p className="login-subtitle">
                {t('reset.accountPrefix')}
                <b className="mono">{account}</b>
              </p>

              <form className="login-form" onSubmit={handleSubmit} noValidate>
                {error ? (
                  <div className="login-error" role="alert">
                    <IconAlert size={16} />
                    <span>{error}</span>
                  </div>
                ) : null}

                <Input
                  label={t('reset.newPassword')}
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={t('reset.newPasswordPlaceholder')}
                  autoComplete="new-password"
                  autoFocus
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
                  label={t('reset.confirmPassword')}
                  type={showPassword ? 'text' : 'password'}
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  placeholder={t('reset.confirmPlaceholder')}
                  autoComplete="new-password"
                  disabled={submitting}
                  prefix={<IconCheck size={15} />}
                  error={errors.confirm}
                  required
                />

                {/* 人机校验：文案复用登录页（同为「验证码 / 拖动滑块」） */}
                {captchaMode === 'image' ? (
                  <div className="login-captcha">
                    <div className="login-captcha-input">
                      <Input
                        label={t('auth.login.captcha')}
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
                ) : captchaMode === 'slider' ? (
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

                <Button
                  type="submit"
                  variant="primary"
                  size="lg"
                  block
                  loading={submitting}
                >
                  {submitting ? t('reset.submitting') : t('reset.submit')}
                </Button>
              </form>
            </>
          ) : null}
    </AuthShell>
  );
}
