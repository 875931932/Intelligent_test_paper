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
import ExamProjectsPage from '@/pages/exam-projects';
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
        <Route path="exam-projects" element={<ExamProjectsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/courses" replace />} />
    </Routes>
  );
}
