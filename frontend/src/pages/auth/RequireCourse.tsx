import { Navigate, useLocation } from 'react-router-dom';
import { useCourseStore } from '../../stores/course';

interface RequireCourseProps {
  children: React.ReactNode;
}

export function RequireCourse({ children }: RequireCourseProps) {
  const activeCourseId = useCourseStore((s) => s.activeCourseId);
  const location = useLocation();

  if (!activeCourseId) {
    return <Navigate to="/courses" replace state={{ from: location }} />;
  }

  return children;
}
