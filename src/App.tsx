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

  return (
    <div className="flex flex-col h-screen w-screen bg-gray-50">
      <header className="flex items-center justify-between px-6 py-3 bg-white border-b border-gray-200 shadow-sm z-10">
        <div className="flex items-center gap-2">
          <div className="w-8 h-8 bg-blue-600 rounded-md flex items-center justify-center text-white font-bold">
            B
          </div>
          <h1 className="text-xl font-semibold text-gray-800">Blueprint</h1>
          <span className="ml-2 px-2 py-0.5 text-xs font-medium bg-blue-100 text-blue-800 rounded-full">
            MVP Sandbox
          </span>
        </div>
        <button 
          onClick={handleDeploy}
          disabled={isDeploying}
          className={`px-4 py-2 rounded-md font-medium text-white transition-colors ${
            isDeploying ? 'bg-blue-400 cursor-not-allowed' : 'bg-blue-600 hover:bg-blue-700'
          }`}
        >
          {isDeploying ? 'Compiling Vision...' : 'Deploy Blueprint'}
        </button>
      </header>
      
      <main className="flex-1 relative">
        <Tldraw persistenceKey="blueprint-canvas" />
      </main>
    </div>
  );
}
