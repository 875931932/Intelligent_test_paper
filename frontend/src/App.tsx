import { Routes, Route, Navigate } from 'react-router-dom';
import { Outlet } from 'react-router-dom';
import { ProtectedRoute } from '@/pages/auth/ProtectedRoute';
import { useAuthStore } from '@/stores/auth';
import { Sidebar } from '@/components/layout/Sidebar';
import { Layout } from '@/components/layout/Layout';
import CourseSpacePage from '@/pages/course-space';
import Dashboard from '@/pages/dashboard';
import Materials from '@/pages/materials';
import Framework from '@/pages/framework';
import Knowledge from '@/pages/knowledge';
import PaperPage from '@/pages/paper';
import { LoginPage } from '@/pages/auth/LoginPage';

function AppShell() {
  const logout = useAuthStore((s) => s.logout);

  return (
    <Layout sidebar={<Sidebar onLogout={logout} />}>
      <Outlet />
    </Layout>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/courses"
        element={
          <ProtectedRoute>
            <CourseSpacePage />
          </ProtectedRoute>
        }
      />
      <Route
        path="/courses/:courseId"
        element={
          <ProtectedRoute>
            <AppShell />
          </ProtectedRoute>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="materials" element={<Materials />} />
        <Route path="framework" element={<Framework />} />
        <Route path="knowledge" element={<Knowledge />} />
        {/* 出卷流水线与试卷查看/审核/导出已合并为同一个「试卷」页面 */}
        <Route path="paper" element={<PaperPage />} />
        <Route path="exam-projects" element={<Navigate to="../paper" replace />} />
        <Route path="paper-center" element={<Navigate to="../paper" replace />} />
      </Route>
      <Route path="*" element={<Navigate to="/courses" replace />} />
    </Routes>
  );
}
