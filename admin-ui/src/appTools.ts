import { registerToolKind } from './toolRegistry';
import WhiteboardTool from './components/WhiteboardTool';
import NotesTool from './components/NotesTool';

registerToolKind('whiteboard', WhiteboardTool);
registerToolKind('notes', NotesTool);
