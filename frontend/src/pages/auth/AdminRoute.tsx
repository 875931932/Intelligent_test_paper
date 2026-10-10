import { Navigate, useLocation } from 'react-router-dom';
import { useAuthStore } from '../../stores/auth';

interface AdminRouteProps {
  children: React.ReactNode;
}

/** 后台路由守卫：仅 admin / org_admin 可进，教师回课程空间。授权以服务端为准，这里只管渲染。 */
export function AdminRoute({ children }: AdminRouteProps) {
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated);
  const role = useAuthStore((s) => s.user?.role);
  const location = useLocation();

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  if (role !== 'admin' && role !== 'org_admin') {
    return <Navigate to="/courses" replace />;
  }
  return <>{children}</>;
}
