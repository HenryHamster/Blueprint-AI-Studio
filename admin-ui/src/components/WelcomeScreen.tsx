import { Plus, ExternalLink, Users } from 'lucide-react';

interface WelcomeScreenProps {
  onCreateProject: () => void;
  onSignOut: () => void;
}

export function WelcomeScreen({ onCreateProject, onSignOut }: WelcomeScreenProps) {
  return (
    <div className="h-screen flex items-center justify-center bg-neutral-950">
      <div className="bg-neutral-900 border border-neutral-800 rounded-xl p-8 text-center max-w-md mx-4">
        <h1 className="text-lg font-semibold text-neutral-100 mb-1">Welcome to Blueprint</h1>
        <p className="text-xs text-neutral-500 mb-6">You don't have access to any projects yet.</p>

        <button
          onClick={onCreateProject}
          className="inline-flex items-center gap-2 bg-white hover:bg-neutral-200 text-neutral-950 px-6 py-2.5 rounded text-sm font-semibold transition-colors w-full justify-center mb-6"
        >
          <Plus className="w-4 h-4" />
          Create your first project
        </button>

        <div className="text-left space-y-3 mb-6">
          <div className="p-3 bg-neutral-800/50 rounded-lg border border-neutral-700/50">
            <p className="text-xs text-neutral-400 leading-relaxed">
              <strong className="text-neutral-300">Before you start:</strong>{' '}
              The platform's GitHub App must be installed on the target repository.{' '}
              <a
                href="https://github.com/HenryHamster/BlueprintAIStudio/blob/main/docs/ONBOARDING.md#1-install-the-github-app-on-the-target-repository"
                target="_blank"
                rel="noopener noreferrer"
                className="text-cyan-400 hover:text-cyan-300 underline inline-flex items-center gap-0.5"
              >
                Setup guide <ExternalLink className="w-3 h-3" />
              </a>
            </p>
          </div>

          <div className="p-3 bg-neutral-800/50 rounded-lg border border-neutral-700/50">
            <p className="text-xs text-neutral-400 leading-relaxed">
              <Users className="w-3.5 h-3.5 inline text-neutral-500 mr-1 -mt-0.5" />
              <strong className="text-neutral-300">Already have a project?</strong>{' '}
              Ask the project owner to add you from Project → Manage → Access.
            </p>
          </div>
        </div>

        <button
          onClick={onSignOut}
          className="text-xs text-neutral-600 hover:text-neutral-400 transition-colors underline"
        >
          Sign out
        </button>
      </div>
    </div>
  );
}
