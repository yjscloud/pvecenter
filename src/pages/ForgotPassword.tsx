/* ==========================================================================
   pvecenter — 忘记密码（自助重置第 1 步）
   填用户名 → 面板给该账号邮箱发一条 15 分钟有效的一次性链接。

   刻意不告诉访问者「这个用户名存不存在」：这是公开页面，
   一旦能区分，就等于给了别人一台账号枚举机。
   ========================================================================== */

import { useCallback, useRef, useState, type FormEvent } from 'react';
import { Link } from 'react-router';
import { useQuery } from '@tanstack/react-query';
import { authApi } from '../api/endpoints';
import { errorMessage } from '../api/client';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Notice } from '../components/ui/EmptyState';
import { SliderCaptcha } from '../components/SliderCaptcha';
import type { SliderTracePoint } from '../api/types';
import { IconAlert, IconShield, IconUser } from '../components/Icons';
import { AuthShell } from '../components/AuthShell';
import { useT } from '../i18n';

export function ForgotPassword() {
  const t = useT();
  const [username, setUsername] = useState('');
  const [captchaCode, setCaptchaCode] = useState('');
  const [errors, setErrors] = useState<Record<string, string | undefined>>({});
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [sent, setSent] = useState(false);

  /*
    人机校验与登录 / 注册共用同一套「设置 → 登录验证」配置。
    单独一个 queryKey：挑战一次性，和登录页共用缓存会互相把对方的题作废。
  */
  const captchaQ = useQuery({
    queryKey: ['forgot-captcha'],
    queryFn: authApi.captcha,
    refetchOnWindowFocus: false,
    staleTime: 0,
    retry: 1,
  });
  const captchaMode =
    captchaQ.data?.mode ?? (captchaQ.data?.required ? 'image' : 'off');
  const captchaRequired = captchaMode !== 'off';

  const sliderXRef = useRef(0);
  const sliderPathRef = useRef<SliderTracePoint[]>([]);
  const handleSliderPosition = useCallback((x: number, path: SliderTracePoint[]) => {
    sliderXRef.current = x;
    sliderPathRef.current = path;
    if (x > 0) {
      setErrors((prev) => (prev.captcha ? { ...prev, captcha: undefined } : prev));
    }
  }, []);

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (submitting) return;

    const next: Record<string, string | undefined> = {};
    if (!username.trim()) {
      next.username = t('forgot.err.usernameRequired');
    }
    if (captchaMode === 'image' && !captchaCode.trim()) {
      next.captcha = t('auth.err.captchaRequired');
    } else if (captchaMode === 'slider' && sliderXRef.current <= 0) {
      next.captcha = t('auth.err.sliderRequired');
    }
    setErrors(next);
    if (Object.keys(next).length > 0) return;

    setError(null);
    setSubmitting(true);
    try {
      await authApi.forgotPassword({
        username: username.trim(),
        captcha_id: captchaRequired ? (captchaQ.data?.id ?? '') : undefined,
        captcha_code:
          captchaMode === 'image' ? captchaCode.trim() : undefined,
        captcha_x: captchaMode === 'slider' ? sliderXRef.current : undefined,
        captcha_path: captchaMode === 'slider' ? sliderPathRef.current : undefined,
      });
      setSent(true);
    } catch (err) {
      const msg = errorMessage(err);
      if (captchaRequired && /验证码|滑块/.test(msg)) {
        setErrors({ captcha: msg });
      } else {
        setError(msg);
      }
      /* 挑战一次性：失败后换一张，顺带清掉上次作答 */
      if (captchaRequired) {
        setCaptchaCode('');
        sliderXRef.current = 0;
        void captchaQ.refetch();
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AuthShell>
          {sent ? (
            <>
              <h1 className="login-title">{t('forgot.sentTitle')}</h1>
              <p className="login-subtitle">{t('forgot.sentSubtitle')}</p>

              <div className="mb-16">
                <Notice tone="success" title={t('forgot.noticeTitle')}>
                  {t('forgot.noticeBodyPre')}
                  <b>{t('forgot.noticeBodyStrong')}</b>
                  {t('forgot.noticeBodyPost')}
                </Notice>
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
            <>
              <h1 className="login-title">{t('forgot.title')}</h1>
              <p className="login-subtitle">{t('forgot.subtitle')}</p>

              <form className="login-form" onSubmit={handleSubmit} noValidate>
                {error ? (
                  <div className="login-error" role="alert">
                    <IconAlert size={16} />
                    <span>{error}</span>
                  </div>
                ) : null}

                <Input
                  label={t('auth.login.username')}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder={t('register.usernamePlaceholder')}
                  autoComplete="username"
                  autoFocus
                  disabled={submitting}
                  prefix={<IconUser size={15} />}
                  error={errors.username}
                  hint={t('forgot.usernameHint')}
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
                  disabled={!username.trim()}
                >
                  {submitting ? t('forgot.submitting') : t('forgot.submit')}
                </Button>
              </form>

              <div className="login-health">
                <div className="login-health-row">
                  <span className="login-health-label">{t('forgot.remembered')}</span>
                  <Link to="/login" className="fs-sm">
                    {t('auth.backToLogin')}
                  </Link>
                </div>
              </div>
            </>
          )}
    </AuthShell>
  );
}
