import { useEffect, useState } from 'react';
import {
  ArrowRight,
  Box,
  Check,
  ChevronRight,
  Command,
  GitBranch,
  GitPullRequest,
  Layers,
  LayoutGrid,
  Play,
  Search,
  Server,
  ShieldCheck,
  Workflow,
  Zap,
} from 'lucide-react';
import { initialTasks, transition } from './model';
import type { DemoAction, DemoTask } from './model';
import { Architecture, Fleet } from './SystemViews';
import { Stat, Status } from './DemoPrimitives';
import { TaskDetail } from './TaskDetail';
import type { Detail } from './TaskDetail';
import { NewTaskForm } from './NewTaskForm';

type View = 'Pipeline' | 'Fleet' | 'Architecture';

export function DemoApp() {
  const [tasks, setTasks] = useState<DemoTask[]>(() =>
    initialTasks.map((task) => ({ ...task })),
  );
  const [selectedId, setSelectedId] = useState('BP-104');
  const [view, setView] = useState<View>('Pipeline');
  const [detail, setDetail] = useState<Detail>('Overview');
  const [query, setQuery] = useState('');
  const [playing, setPlaying] = useState(false);
  const [creating, setCreating] = useState(false);
  const selected = tasks.find((task) => task.id === selectedId) ?? tasks[0];

  function act(action: DemoAction) {
    setTasks((current) =>
      current.map((task) =>
        task.id === selectedId ? transition(task, action) : task,
      ),
    );
  }

  useEffect(() => {
    if (!playing) return;
    const timer = window.setTimeout(() => {
      setTasks((current) =>
        current.map((task) =>
          task.id === selectedId ? transition(task, 'advance') : task,
        ),
      );
      if (selected.status === 'IN_PROGRESS') setPlaying(false);
    }, 1300);
    return () => window.clearTimeout(timer);
  }, [playing, selectedId, selected.status]);

  function replay() {
    setTasks((current) =>
      current.map((task) =>
        task.id === selectedId
          ? { ...task, status: 'QUEUED', attempt: 1 }
          : task,
      ),
    );
    setDetail('Execution log');
    setPlaying(true);
  }

  const filteredTasks = tasks.filter((task) =>
    `${task.title} ${task.id} ${task.type}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  const active = tasks.filter((task) =>
    ['PROVISIONING', 'IN_PROGRESS'].includes(task.status),
  ).length;

  return (
    <div className="demo-shell">
      <a className="skip-link" href="#workspace">
        Skip to workspace
      </a>
      <aside className="sidebar">
        <a className="brand" href="./" aria-label="Blueprint demo home">
          <span className="brand-mark">
            <Layers size={22} />
          </span>
          blueprint<span className="brand-dot">.</span>
        </a>
        <div className="workspace-picker">
          <span className="workspace-avatar">S</span>
          <span>
            Sample workspace<small>Personal project</small>
          </span>
          <ChevronRight size={15} />
        </div>
        <p className="nav-label">WORKSPACE</p>
        <nav aria-label="Workspace">
          {(
            [
              { name: 'Pipeline', icon: LayoutGrid },
              { name: 'Fleet', icon: Server },
              { name: 'Architecture', icon: Workflow },
            ] as const
          ).map(({ name, icon: Icon }) => (
              <button
                key={name}
                aria-label={name}
                className={`nav-item ${view === name ? 'selected' : ''}`}
              aria-current={view === name ? 'page' : undefined}
              onClick={() => setView(name)}
            >
              <Icon size={18} />
              {name}
              {name === 'Pipeline' && (
                <span className="nav-count">{tasks.length}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-note">
          <Box size={19} />
          <strong>A system you can explore.</strong>
          <p>Follow a task from an idea to a reviewed change.</p>
          <span>Python · TypeScript · React</span>
        </div>
        <div className="sidebar-footer">
          <span className="avatar">BP</span>
          <span>
            Blueprint AI Studio<small>Portfolio edition</small>
          </span>
        </div>
      </aside>

      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            Workspace
            <ChevronRight size={14} />
            <strong>{view}</strong>
          </div>
          <span className="local-indicator">
            <span />
            Runs in your browser
          </span>
        </header>
        <main id="workspace" tabIndex={-1}>
          <div className="demo-banner">
            <span>
              <span className="demo-label">INTERACTIVE DEMO</span>Sample data.
              Execution and checks are simulated.
            </span>
            <button
              onClick={() => {
                setView('Pipeline');
                replay();
              }}
            >
              <Play size={13} />
              Replay a task
            </button>
          </div>
          <div className="page-heading">
            <div>
              <p className="eyebrow">THE AGENT CONTROL PLANE</p>
              <h1>
                {view === 'Pipeline'
                  ? 'From intent to implementation.'
                  : view === 'Fleet'
                    ? 'Execution, with boundaries.'
                    : 'The system behind the work.'}
              </h1>
              <p>
                {view === 'Pipeline'
                  ? 'Give agents a task. Keep the process observable, bounded, and reviewable.'
                  : view === 'Fleet'
                    ? 'A sample fleet showing how work is placed and isolated.'
                    : 'A guided map of the implementation in this repository.'}
              </p>
            </div>
            <span className="project-chip">
              <GitBranch size={15} />
              sample / observatory
            </span>
          </div>

          {view === 'Pipeline' ? (
            <>
              <section className="stats" aria-label="Sample task statistics">
                <Stat
                  label="Tasks in workspace"
                  value={tasks.length}
                  note="One focused sample project"
                  icon={<Layers size={17} />}
                />
                <Stat
                  label="Currently executing"
                  value={active}
                  note="Isolated agent workspaces"
                  icon={<Zap size={17} />}
                />
                <Stat
                  label="Awaiting review"
                  value={
                    tasks.filter((task) => task.status === 'EVALUATING').length
                  }
                  note="Approval stays with you"
                  icon={<GitPullRequest size={17} />}
                />
                <Stat
                  label="Completed"
                  value={
                    tasks.filter((task) => task.status === 'COMPLETED').length
                  }
                  note="Deployment verified separately"
                  icon={<Check size={17} />}
                />
              </section>
              <section className="pipeline-panel" aria-label="Task pipeline">
                <div className="panel-header">
                  <div>
                    <h2>Task pipeline</h2>
                    <span className="subtle">
                      A small change. An inspectable process.
                    </span>
                  </div>
                  <div className="pipeline-tools">
                    <button
                      className="button primary"
                      onClick={() => setCreating((current) => !current)}
                    >
                      New sample task
                    </button>
                    <label className="search">
                      <Search size={15} />
                      <input
                        aria-label="Search tasks"
                        value={query}
                        onChange={(event) => setQuery(event.target.value)}
                        placeholder="Search tasks…"
                      />
                      <kbd>⌕</kbd>
                    </label>
                  </div>
                </div>
                {creating && (
                  <NewTaskForm
                    onCancel={() => setCreating(false)}
                    onCreate={(title, description) => {
                      const id = `BP-${106 + tasks.length - initialTasks.length}`;
                      setTasks((current) => [
                        {
                          id,
                          title,
                          description,
                          type: 'Feature',
                          status: 'QUEUED',
                          branch: `agent/sample-task-${id.toLowerCase()}`,
                          attempt: 1,
                        },
                        ...current,
                      ]);
                      setSelectedId(id);
                      setQuery('');
                      setDetail('Overview');
                      setPlaying(false);
                      setCreating(false);
                    }}
                  />
                )}
                <div className="pipeline-split">
                  <div className="task-list" aria-label="Tasks">
                    <div className="list-label">
                      <span>RECENT TASKS</span>
                      <span>{filteredTasks.length}</span>
                    </div>
                    {filteredTasks.map((task) => (
                      <button
                        key={task.id}
                        aria-pressed={selectedId === task.id}
                        className={`task-row ${selectedId === task.id ? 'active' : ''}`}
                        onClick={() => {
                          setSelectedId(task.id);
                          setPlaying(false);
                          setDetail('Overview');
                        }}
                      >
                        <span className="task-meta">
                          <span>{task.id}</span>
                          <Status task={task} />
                        </span>
                        <strong>{task.title}</strong>
                        <span className="task-bottom">
                          <span>{task.type}</span>
                          <ArrowRight size={15} />
                        </span>
                      </button>
                    ))}
                    {filteredTasks.length === 0 && (
                      <p className="empty-state">No tasks match “{query}”.</p>
                    )}
                    <div className="list-foot">
                      <ShieldCheck size={15} />
                      Every change has a review boundary.
                    </div>
                  </div>
                  <TaskDetail
                    selected={selected}
                    detail={detail}
                    playing={playing}
                    setDetail={setDetail}
                    setPlaying={setPlaying}
                    replay={replay}
                    act={act}
                  />
                </div>
              </section>
              <div className="bottom-note">
                <span>
                  <Command size={15} />
                  Built around a simple principle
                </span>
                <p>Agents do the work. The system owns the lifecycle.</p>
                <button onClick={() => setView('Architecture')}>
                  Explore the architecture
                  <ArrowRight size={15} />
                </button>
              </div>
            </>
          ) : view === 'Fleet' ? (
            <Fleet active={active} />
          ) : (
            <Architecture />
          )}
          <footer className="page-footer">
            <span>BLUEPRINT / ENGINEERING EXPLORATION</span>
            <span>
              Local simulation · no credentials · no external requests
            </span>
          </footer>
        </main>
      </div>
    </div>
  );
}
