export interface StoredPendingAttachment {
  id: string;
  file: File;
  mime_type: string;
  name: string;
  size_bytes: number;
}

export declare function putPendingAttachment(
  actorUserId: string,
  projectId: string,
  sendKey: string,
  attachment: StoredPendingAttachment,
): Promise<void>;

export declare function deletePendingAttachment(
  actorUserId: string,
  projectId: string,
  sendKey: string,
  attachmentId: string,
): Promise<void>;

export declare function clearPendingAttachments(
  actorUserId: string,
  projectId: string,
  sendKey: string,
): Promise<void>;

export declare function listPendingAttachments(
  actorUserId: string,
  projectId: string,
  sendKey: string,
): Promise<StoredPendingAttachment[]>;

export declare function getPendingAttachment(
  actorUserId: string,
  projectId: string,
  sendKey: string,
  attachmentId: string,
): Promise<StoredPendingAttachment | undefined>;
