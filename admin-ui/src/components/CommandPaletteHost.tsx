import { CommandPalette, type WorkbenchCommand } from './CommandPalette';

interface CommandPaletteHostProps {
  open: boolean;
  commands: WorkbenchCommand[];
  query: string;
  onQueryChange: (query: string) => void;
  setOpen: (open: boolean) => void;
}

export function CommandPaletteHost({
  open,
  commands,
  query,
  onQueryChange,
  setOpen,
}: CommandPaletteHostProps) {
  return (
    <CommandPalette
      open={open}
      commands={commands}
      query={query}
      onQueryChange={onQueryChange}
      onClose={() => setOpen(false)}
    />
  );
}
