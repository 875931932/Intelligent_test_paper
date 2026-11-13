import { Suspense } from 'react';
import { Routes, Route, Navigate, Outlet, useParams } from 'react-router-dom';
import { ProtectedRoute } from '@/pages/auth/ProtectedRoute';
import { AdminRoute } from '@/pages/auth/AdminRoute';
import { useAuthStore } from '@/stores/auth';
import { Sidebar } from '@/components/layout/Sidebar';
import { AdminSidebar } from '@/components/layout/AdminSidebar';
import { Layout } from '@/components/layout/Layout';
import { lazyWithRetry } from '@/lib/lazyWithRetry';

/* 路由级代码分割：首屏只带外壳与共享 UI，九个页面按需加载，
   避免单 bundle 越过 500kB 警戒线（vite 打包警告的根治路径）。
   走 lazyWithRetry：链路偶发 RST/挂起导致 chunk 拉取失败时自动换新连接
   重试、必要时整页重载，不再让用户手动刷新。 */
const CourseSpacePage = lazyWithRetry(() => import('@/pages/course-space'));
/* 统一 AI 助手首页（/home）：课程与共享资料历史、范围选择与对话 */
const HomeAssistantPage = lazyWithRetry(() => import('@/pages/home'));
/* 校级教师共享知识库（/knowledge）：空间列表 + 空间详情（资料/成员/策略） */
const KnowledgeCenterPage = lazyWithRetry(() => import('@/pages/knowledge-center'));
const KnowledgeSpaceDetailPage = lazyWithRetry(() =>
  import('@/pages/knowledge-center/SpacePage')
);
const Dashboard = lazyWithRetry(() => import('@/pages/dashboard'));
const Materials = lazyWithRetry(() => import('@/pages/materials'));
const Framework = lazyWithRetry(() => import('@/pages/framework'));
const Knowledge = lazyWithRetry(() => import('@/pages/knowledge'));
const PaperPage = lazyWithRetry(() => import('@/pages/paper'));
/* 资料库「试卷」文件夹的归档快照编辑页（与试卷页同款双栏阅读器） */
const PaperArchivePage = lazyWithRetry(() => import('@/pages/paper-archive'));
const LoginPage = lazyWithRetry(() =>
  import('@/pages/auth/LoginPage').then((m) => ({ default: m.LoginPage }))
);
/* 后台管理（/admin）：概览 + 组织账号 + 模型管理 + 调用监控；仅 admin/org_admin 可进 */
const AdminOverview = lazyWithRetry(() => import('@/pages/admin'));
const AdminOrganizations = lazyWithRetry(() => import('@/pages/admin/organizations'));
const AdminUsers = lazyWithRetry(() => import('@/pages/admin/users'));
const AdminModels = lazyWithRetry(() => import('@/pages/admin/models'));
const AdminMonitoring = lazyWithRetry(() => import('@/pages/admin/monitoring'));

/** 懒加载路由占位：由最近的 Suspense 边界接管（内容区或独立页） */
function RouteFallback() {
  return (
    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: '260px' }}>
      <div className="spinner spinner-lg" />
    </div>
  );
}

/**
 * 旧课程内 AI 助手地址回收：统一走 /home 并按课程上下文直达。
 * 助手只有一个页面（首页），不再保留第二套课程内对话页。
 */
function CourseAssistantRedirect() {
  const { courseId } = useParams<{ courseId: string }>();
  return <Navigate to={courseId ? `/home?course=${courseId}` : '/home'} replace />;
}

function AppShell() {
  const logout = useAuthStore((s) => s.logout);

  return (
    <Layout sidebar={<Sidebar onLogout={logout} />}>
      {/* 内容区独立边界：切页懒加载时侧栏保持在场，只换内容 */}
      <Suspense fallback={<RouteFallback />}>
        <Outlet />
      </Suspense>
    </Layout>
  );
}

/** 后台管理外壳：AdminSidebar + 内容区（与业务端同布局机制，路由组级复用） */
function AdminShell() {
  const logout = useAuthStore((s) => s.logout);

  return (
    <Layout sidebar={<AdminSidebar onLogout={logout} />}>
      <Suspense fallback={<RouteFallback />}>
        <Outlet />
      </Suspense>
    </Layout>
  );
}

export default function App() {
  return (
    <Suspense fallback={<RouteFallback />}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route
          path="/home"
          element={
            <ProtectedRoute>
              <HomeAssistantPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/courses"
          element={
            <ProtectedRoute>
              <CourseSpacePage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/knowledge"
          element={
            <ProtectedRoute>
              <KnowledgeCenterPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/knowledge/:spaceId"
          element={
            <ProtectedRoute>
              <KnowledgeSpaceDetailPage />
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
          {/* 归档快照编辑：资料库「试卷」文件夹点「编辑」进来，只改副本 */}
          <Route path="paper-archive/:archiveId" element={<PaperArchivePage />} />
          {/* 旧 AI 助手地址：回收为首页 + 课程上下文（单一助手入口） */}
          <Route path="assistant" element={<CourseAssistantRedirect />} />
          <Route path="exam-projects" element={<Navigate to="../paper" replace />} />
          <Route path="paper-center" element={<Navigate to="../paper" replace />} />
        </Route>
        {/* 后台管理：仅 admin/org_admin（AdminRoute 守卫；授权以后端为准） */}
        <Route
          path="/admin"
          element={
            <AdminRoute>
              <AdminShell />
            </AdminRoute>
          }
        >
          <Route index element={<AdminOverview />} />
          <Route path="organizations" element={<AdminOrganizations />} />
          <Route path="users" element={<AdminUsers />} />
          <Route path="llm" element={<AdminModels />} />
          <Route path="monitoring" element={<AdminMonitoring />} />
        </Route>
        <Route path="*" element={<Navigate to="/home" replace />} />
      </Routes>
    </Suspense>
  );
}
