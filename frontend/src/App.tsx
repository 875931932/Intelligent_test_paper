import { Routes, Route, Navigate } from 'react-router-dom';
import { Outlet } from 'react-router-dom';
import { ProtectedRoute } from '@/pages/auth/ProtectedRoute';
import { useAuthStore } from '@/stores/auth';
import { useCourseStore } from '@/stores/course';
import { useToastStore } from '@/stores/toast';
import { api } from '@/api/client';
import { Sidebar } from '@/components/layout/Sidebar';
import { Layout } from '@/components/layout/Layout';
import Dashboard from '@/pages/dashboard';
import Materials from '@/pages/materials';
import Framework from '@/pages/framework';
import Knowledge from '@/pages/knowledge';
import ExamProjectsPage from '@/pages/exam-projects';
import { LoginPage } from '@/pages/auth/LoginPage';

function AppShell() {
  const logout = useAuthStore((s) => s.logout);
  const courses = useCourseStore((s) => s.courses);
  const activeCourseId = useCourseStore((s) => s.activeCourseId);
  const setActiveCourse = useCourseStore((s) => s.setActiveCourse);
  const addToast = useToastStore((s) => s.addToast);

  const handleCreateCourse = async (name: string) => {
    try {
      const course = await api.courses.create({ name });
      // courses should be refreshed from API
      useCourseStore.getState().addCourse(course);
      useCourseStore.getState().setActiveCourse(course.id);
      addToast('课程创建成功');
    } catch (e) {
      addToast('创建课程失败', 'error');
    }
  };

  return (
    <Layout sidebar={
      <Sidebar
        courses={courses}
        activeCourseId={activeCourseId}
        onSelectCourse={setActiveCourse}
        onLogout={logout}
        onCreateCourse={handleCreateCourse}
      />
    }>
      <Outlet />
    </Layout>
  );
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        path="/"
        element={
          <ProtectedRoute>
            <AppShell />
          </ProtectedRoute>
        }
      >
        <Route index element={<Dashboard />} />
        <Route path="courses/:courseId/materials" element={<Materials />} />
        <Route path="courses/:courseId/framework" element={<Framework />} />
        <Route path="courses/:courseId/knowledge" element={<Knowledge />} />
        <Route path="courses/:courseId/exam-projects" element={<ExamProjectsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
