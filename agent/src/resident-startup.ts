export function shouldDeferProviderValidationToResident(
  preClaimTaskType = process.env.BLUEPRINT_PRE_CLAIM_TASK_TYPE,
  preClaimResidentSession = process.env.BLUEPRINT_PRE_CLAIM_RESIDENT_SESSION,
): boolean {
  return preClaimTaskType === 'strategic_review' && preClaimResidentSession === '1';
}
