export async function refreshAndSelectProject(
  projectId: string,
  refreshProjects: () => Promise<ReadonlyArray<{ id: string }> | null | undefined>,
  selectProject: (projectId: string) => void,
): Promise<boolean> {
  const projects = await refreshProjects();
  if (!projects?.some(project => project.id === projectId)) return false;
  selectProject(projectId);
  return true;
}
