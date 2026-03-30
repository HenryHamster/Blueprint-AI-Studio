/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import { Tldraw } from 'tldraw';
import 'tldraw/tldraw.css';
import { useState } from 'react';

export default function App() {
  const [isDeploying, setIsDeploying] = useState(false);

  const handleDeploy = () => {
    setIsDeploying(true);
    // Simulate compilation and deployment to the backend Orchestrator
    setTimeout(() => {
      alert("Blueprint deployed to Orchestrator! VMs are provisioning.");
      setIsDeploying(false);
    }, 2000);
  };

  const handleMount = (editor: any) => {
    editor.user.updateUserPreferences({ colorScheme: 'dark' });
  };

  return (
    <div className="flex flex-col h-screen w-screen bg-neutral-950 text-neutral-200 font-sans">
      <header className="flex items-center justify-between px-4 py-2 border-b border-neutral-800 bg-neutral-900 z-10">
        <div className="flex items-center gap-3">
          <div className="w-5 h-5 bg-white rounded-sm flex items-center justify-center text-neutral-950 text-[10px] font-bold font-mono">
            BP
          </div>
          <h1 className="text-xs font-semibold tracking-tight text-neutral-100">Blueprint</h1>
          <div className="h-3 w-px bg-neutral-700 mx-1"></div>
          <span className="text-[10px] uppercase tracking-wider font-medium text-neutral-400">
            Sandbox Environment
          </span>
        </div>
        <button 
          onClick={handleDeploy}
          disabled={isDeploying}
          className={`px-3 py-1 text-[11px] font-medium rounded-sm transition-colors border ${
            isDeploying 
              ? 'bg-neutral-800 text-neutral-500 border-neutral-700 cursor-not-allowed' 
              : 'bg-neutral-800 text-neutral-200 border-neutral-700 hover:bg-neutral-700 hover:text-white shadow-sm'
          }`}
        >
          {isDeploying ? 'Deploying...' : 'Deploy'}
        </button>
      </header>
      
      <main className="flex-1 relative">
        <Tldraw persistenceKey="blueprint-canvas" onMount={handleMount} />
      </main>
    </div>
  );
}
