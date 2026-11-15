import { useRef } from "react";
import Icon from "./Icon";
import Modal from "./Modal";
import "./sessionDelete.css";

/** 批量删除前的确认弹窗：默认焦点停在“保留”，避免误触回车删掉一整批。 */
export default function SessionBatchDeleteDialog({
  count,
  busy,
  error,
  onConfirm,
  onClose,
}: {
  count: number;
  busy: boolean;
  error?: string | null;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const keepRef = useRef<HTMLButtonElement>(null);
  return (
    <Modal title="批量删除选购记录" onClose={busy ? () => {} : onClose} initialFocusRef={keepRef}>
      <div className="session-delete">
        <span className="session-delete-mark" aria-hidden="true"><Icon name="trash" /></span>
        <p className="session-delete-eyebrow">批量删除</p>
        <h2>将删除 {count} 段选购记录</h2>
        <p className="session-delete-note">
          这些选购的对话、商品结果和上下文记忆会一起删除，删除后无法恢复。正在执行的会话会被跳过，已下的订单与心选收藏不受影响。
        </p>
        {error && <p className="session-delete-error" role="alert">{error}</p>}
        <div className="session-delete-actions">
          <button ref={keepRef} type="button" className="session-delete-keep" onClick={onClose} disabled={busy}>
            保留记录
          </button>
          <button type="button" className="session-delete-confirm" onClick={onConfirm} disabled={busy}>
            {busy ? "正在删除…" : `确认删除 ${count} 段`}
          </button>
        </div>
      </div>
    </Modal>
  );
}
