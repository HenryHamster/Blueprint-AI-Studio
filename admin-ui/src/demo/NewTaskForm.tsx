import { useState } from 'react';

interface Props {
  onCreate: (title: string, description: string) => void;
  onCancel: () => void;
}

export function NewTaskForm({ onCreate, onCancel }: Props) {
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  return (
    <form
      className="new-task-form"
      aria-label="Create a sample task"
      onSubmit={(event) => {
        event.preventDefault();
        if (title.trim() && description.trim())
          onCreate(title.trim(), description.trim());
      }}
    >
      <div>
        <h3>Create a sample task</h3>
        <p>
          Your prompt stays in this browser session. Refresh to reset the demo.
        </p>
      </div>
      <label>
        Task title
        <input
          required
          maxLength={120}
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder="Add a health check endpoint"
        />
      </label>
      <label>
        Goal
        <textarea
          required
          maxLength={1000}
          rows={3}
          value={description}
          onChange={(event) => setDescription(event.target.value)}
          placeholder="Describe the change and what a successful result looks like."
        />
      </label>
      <div className="form-actions">
        <button type="button" className="button secondary" onClick={onCancel}>
          Cancel
        </button>
        <button
          className="button primary"
          type="submit"
          disabled={!title.trim() || !description.trim()}
        >
          Queue sample task
        </button>
      </div>
    </form>
  );
}
