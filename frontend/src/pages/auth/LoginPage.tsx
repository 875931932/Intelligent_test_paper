import { useState, type FormEvent } from 'react';
import { BookOpen } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { api } from '@/api/client';
import { ApiError } from '@/api/errors';
import { useAuthStore } from '../../stores/auth';
import { useToastStore } from '../../stores/toast';
import { Button } from '../../components/ui/Button';
import { Input } from '../../components/ui/Input';

export function LoginPage() {
  const navigate = useNavigate();
  const setAuth = useAuthStore((s) => s.setAuth);
  const addToast = useToastStore((s) => s.addToast);

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [usernameError, setUsernameError] = useState('');
  const [passwordError, setPasswordError] = useState('');

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();

    let valid = true;
    if (!username.trim()) {
      setUsernameError('请输入用户名');
      valid = false;
    } else {
      setUsernameError('');
    }
    if (!password.trim()) {
      setPasswordError('请输入密码');
      valid = false;
    } else {
      setPasswordError('');
    }
    if (!valid) return;

    setLoading(true);
    try {
      const { token, user } = await api.auth.login(username.trim(), password);
      setAuth(
        { id: user.id, username: user.username, name: user.name, role: user.role === 'admin' ? 'admin' : 'teacher' },
        token
      );
      addToast('登录成功，欢迎回来！', 'success');
      navigate('/courses', { replace: true });
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : '登录失败，请稍后再试';
      addToast(msg, 'error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-page">
      <div className="login-orbs">
        <span className="orb orb-1" />
        <span className="orb orb-2" />
        <span className="orb orb-3" />
      </div>

      <div className="login-card glass-card animate-fade-in">
        <div className="login-header">
          <div className="login-icon">
            <BookOpen size={28} strokeWidth={1.8} />
          </div>
          <h1 className="login-title">智能出卷</h1>
          <p className="login-subtitle">AI 驱动的智能试卷生成系统</p>
        </div>

        <form className="login-form" onSubmit={handleSubmit}>
          <Input
            label="用户名"
            type="text"
            placeholder="请输入用户名"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            error={usernameError}
            autoComplete="username"
            autoFocus
          />
          <Input
            label="密码"
            type="password"
            placeholder="请输入密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            error={passwordError}
            autoComplete="current-password"
          />
          <Button
            type="submit"
            variant="primary"
            size="lg"
            className="login-btn"
            disabled={loading}
          >
            {loading && <span className="spinner" />}
            {loading ? '登录中…' : '登录'}
          </Button>
        </form>

        <p className="login-hint">
          测试账号：admin / 123456
        </p>
      </div>

      <style>{loginStyles}</style>
    </div>
  );
}

const loginStyles = `
  .login-page {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    position: relative;
    overflow: hidden;
    background: linear-gradient(135deg, #f2f2f7 0%, #e8e8ed 50%, #f0f0f5 100%);
  }

  .login-orbs {
    position: absolute;
    inset: 0;
    pointer-events: none;
    overflow: hidden;
  }

  .orb {
    position: absolute;
    border-radius: 50%;
    filter: blur(80px);
    opacity: 0.45;
    animation: orbFloat 12s ease-in-out infinite;
  }

  .orb-1 {
    width: 400px;
    height: 400px;
    background: radial-gradient(circle, rgba(0, 113, 227, 0.08), transparent 70%);
    top: -10%;
    left: -5%;
    animation-delay: 0s;
  }

  .orb-2 {
    width: 350px;
    height: 350px;
    background: radial-gradient(circle, rgba(0, 113, 227, 0.06), transparent 70%);
    bottom: -10%;
    right: -5%;
    animation-delay: -4s;
    animation-duration: 14s;
  }

  .orb-3 {
    width: 280px;
    height: 280px;
    background: radial-gradient(circle, rgba(0, 113, 227, 0.05), transparent 70%);
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    animation-delay: -8s;
    animation-duration: 16s;
  }

  @keyframes orbFloat {
    0%, 100% { transform: translate(0, 0) scale(1); }
    25% { transform: translate(60px, -40px) scale(1.1); }
    50% { transform: translate(-30px, 50px) scale(0.9); }
    75% { transform: translate(-50px, -30px) scale(1.05); }
  }

  .login-card {
    position: relative;
    z-index: 1;
    width: 100%;
    max-width: 420px;
    padding: 40px 36px;
    margin: 0 16px;
  }

  .login-header {
    text-align: center;
    margin-bottom: 32px;
  }

  .login-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 56px;
    height: 56px;
    border-radius: 16px;
    background: rgba(0, 113, 227, 0.1);
    color: #0071e3;
    margin-bottom: 16px;
  }

  .login-title {
    font-size: 1.75rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    color: var(--text);
    margin: 0;
  }

  .login-subtitle {
    font-size: 0.875rem;
    color: var(--text-secondary);
    margin-top: 6px;
  }

  .login-form {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  .login-btn {
    margin-top: 4px;
    width: 100%;
  }

  .login-hint {
    text-align: center;
    font-size: 0.75rem;
    color: var(--text-tertiary);
    margin-top: 20px;
  }
`;
