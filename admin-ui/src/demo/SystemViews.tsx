import {
  ArrowRight,
  Check,
  Cpu,
  Database,
  GitPullRequest,
  Layers,
  LockKeyhole,
  Server,
  ShieldCheck,
  Terminal,
} from 'lucide-react';

export function Fleet({ active }: { active: number }) {
  return (
    <section className="system-panel">
      <div className="panel-header">
        <div>
          <h2>Runner fleet</h2>
          <span className="subtle">
            Illustrative capacity and execution boundaries
          </span>
        </div>
        <span className="sample-tag">SAMPLE TOPOLOGY</span>
      </div>
      <div className="fleet-grid">
        {[
          {
            name: 'local-runner-01',
            kind: 'Self-hosted Linux',
            icon: Server,
            workload: `${active} active sample task${active === 1 ? '' : 's'}`,
            detail:
              'Agent work runs in an isolated workspace. Heartbeats make a missing worker visible.',
          },
          {
            name: 'cloud-runner-02',
            kind: 'GCP virtual machine',
            icon: Cpu,
            workload: 'Standby',
            detail:
              'Disposable compute provides a separate execution boundary for repository tasks.',
          },
        ].map(({ name, kind, icon: Icon, workload, detail }) => (
          <article className="runner-card" key={name}>
            <div className="runner-icon">
              <Icon size={24} />
            </div>
            <span className="status status-completed">
              <span />
              Sample runner
            </span>
            <h3>{name}</h3>
            <p>{kind}</p>
            <div className="runner-workload">
              <Terminal size={16} />
              {workload}
            </div>
            <p>{detail}</p>
            <div className="runner-footer">
              <LockKeyhole size={14} />
              Task-scoped access
              <Check size={14} />
            </div>
          </article>
        ))}
      </div>
      <div className="system-callout">
        <ShieldCheck size={24} />
        <div>
          <h3>Loss of a worker is a lifecycle event.</h3>
          <p>
            Return to Pipeline, select a running task, and choose “Interrupt
            worker”. The simulation stops until you retry. The production
            implementation adds heartbeat timeouts, retry budgets, and recovery
            sweeps.
          </p>
        </div>
      </div>
    </section>
  );
}

export function Architecture() {
  return (
    <section className="system-panel">
      <div className="panel-header">
        <div>
          <h2>One control plane. Explicit boundaries.</h2>
          <span className="subtle">
            Implementation map · see docs/portfolio/ARCHITECTURE.md
          </span>
        </div>
        <span className="sample-tag">SOURCE GUIDE</span>
      </div>
      <div className="architecture-flow">
        {[
          {
            name: 'Operator workspace',
            icon: Layers,
            path: 'admin-ui/src/',
            text: 'Tasks, conversations, fleet visibility, and review decisions.',
          },
          {
            name: 'Control plane',
            icon: Database,
            path: 'backend/app/',
            text: 'Persisted state, scheduling, permissions, budgets, and recovery.',
          },
          {
            name: 'Execution runtime',
            icon: Terminal,
            path: 'agent/src/ + scripts/',
            text: 'Provider execution in local runners or cloud compute.',
          },
          {
            name: 'Review & release',
            icon: GitPullRequest,
            path: 'backend/app/release_*',
            text: 'Review, CI, promotion, deployment, and serving verification.',
          },
        ].map(({ name, icon: Icon, path, text }, index) => (
          <div className="architecture-node" key={name}>
            <div>
              <span className="node-number">0{index + 1}</span>
              <Icon size={22} />
            </div>
            <h3>{name}</h3>
            <p>{text}</p>
            <code>{path}</code>
            {index < 3 && <ArrowRight className="node-arrow" size={18} />}
          </div>
        ))}
      </div>
      <div className="principles">
        {[
          [
            'State outlives processes',
            'Workers can disappear. Durable task state lets the control plane inspect and recover unfinished work.',
          ],
          [
            'Authority stays explicit',
            'Agent completion, operator approval, and permission to merge are distinct decisions.',
          ],
          [
            'Evidence precedes release',
            'A merged change is not proof of a deployment. Serving observations form a separate gate.',
          ],
        ].map(([title, text]) => (
          <article key={title}>
            <ShieldCheck size={20} />
            <h3>{title}</h3>
            <p>{text}</p>
          </article>
        ))}
      </div>
      <div className="system-callout">
        <LockKeyhole size={24} />
        <div>
          <h3>What this demo proves</h3>
          <p>
            You can explore a representative workflow without credentials or
            infrastructure. It is a curated presentation surface, not the
            production dashboard or a live orchestration run. Backend behavior
            is verified by its own tests and integration checks.
          </p>
        </div>
      </div>
    </section>
  );
}
