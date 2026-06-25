import { ProjectIntegrationBranchSection } from './ProjectIntegrationBranchSection';
import { ProjectPromotionTargetSection } from './ProjectPromotionTargetSection';

interface ProjectBranchesSectionProps {
  integrationBranchData: { integration_branch: string; is_default: boolean } | null;
  onSaveIntegrationBranch: (branch: string | null) => Promise<void>;
  promotionTargetData: { promotion_target: string; is_default: boolean } | null;
  onSavePromotionTarget: (branch: string | null) => Promise<void>;
  canSave: boolean;
}

export function ProjectBranchesSection({
  integrationBranchData,
  onSaveIntegrationBranch,
  promotionTargetData,
  onSavePromotionTarget,
  canSave,
}: ProjectBranchesSectionProps) {
  return (
    <div className="space-y-8">
      <ProjectIntegrationBranchSection
        data={integrationBranchData}
        canSave={canSave}
        onSave={onSaveIntegrationBranch}
        relatedPromotionTarget={promotionTargetData?.promotion_target}
      />
      <hr className="border-neutral-800" />
      <ProjectPromotionTargetSection
        data={promotionTargetData}
        canSave={canSave}
        onSave={onSavePromotionTarget}
        relatedIntegrationBranch={integrationBranchData?.integration_branch}
      />
    </div>
  );
}
