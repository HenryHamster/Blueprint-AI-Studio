export interface ComposerRequestOwner {
  actor_user_id: string | null;
  project_id: string;
  generation: number;
}

export class ComposerRequestOwnershipGuard {
  private generation = 0;
  private owner: ComposerRequestOwner | null = null;

  get active(): boolean { return this.owner != null; }

  begin(actorUserId: string | null, projectId: string): ComposerRequestOwner {
    const owner = { actor_user_id: actorUserId, project_id: projectId, generation: ++this.generation };
    this.owner = owner;
    return owner;
  }

  reset(): void {
    this.generation += 1;
    this.owner = null;
  }

  finish(candidate: ComposerRequestOwner): boolean {
    if (!this.owner
      || this.owner.actor_user_id !== candidate.actor_user_id
      || this.owner.project_id !== candidate.project_id
      || this.owner.generation !== candidate.generation) return false;
    this.owner = null;
    return true;
  }
}
