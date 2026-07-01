import { useCallback, useEffect, useState } from "react";
import { Globe } from "lucide-react";
import type { Memory } from "../../types";
import { api } from "../../lib/api";
import { useToast } from "../ui/Toast";
import { RulesTab } from "../RulesTab";
import { ConfirmDialog } from "../ConfirmDialog";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "../ui/Dialog";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { Label } from "../ui/Label";
import { Textarea } from "../ui/Textarea";

// The reserved project that holds org-wide rules. Approve/revoke reuse the
// per-project rule endpoints with this slug.
const ORG_SLUG = "__global__";

type EditorState = {
  open: boolean;
  ruleType: "mandatory" | "forbidden";
  id?: string;
  title: string;
  content: string;
};

const CLOSED: EditorState = {
  open: false,
  ruleType: "mandatory",
  title: "",
  content: "",
};

export function OrgRulesView() {
  const { toast } = useToast();
  const [mandatory, setMandatory] = useState<Memory[]>([]);
  const [forbidden, setForbidden] = useState<Memory[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editor, setEditor] = useState<EditorState>(CLOSED);
  const [saving, setSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Memory | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.listOrgRules();
      setMandatory(res.mandatory_rules);
      setForbidden(res.forbidden_rules);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load org rules");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!editor.title.trim() || !editor.content.trim() || saving) return;
    setSaving(true);
    try {
      if (editor.id) {
        await api.updateOrgRule(editor.id, {
          title: editor.title.trim(),
          content: editor.content.trim(),
        });
      } else {
        await api.createOrgRule({
          rule_type: editor.ruleType,
          title: editor.title.trim(),
          content: editor.content.trim(),
        });
      }
      toast({ title: "Org rule saved", variant: "success" });
      setEditor(CLOSED);
      void load();
    } catch (err) {
      toast({
        title: "Failed to save rule",
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    } finally {
      setSaving(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    try {
      await api.deleteOrgRule(deleteTarget.id);
      toast({ title: "Org rule deleted", variant: "success" });
      setDeleteTarget(null);
      void load();
    } catch (err) {
      toast({
        title: "Failed to delete rule",
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    } finally {
      setDeleteBusy(false);
    }
  };

  const govern = async (memory: Memory, action: "approve" | "revoke") => {
    try {
      if (action === "approve") await api.approveRule(ORG_SLUG, memory.id);
      else await api.revokeRule(ORG_SLUG, memory.id);
      toast({ title: `Rule ${action}d`, variant: "success" });
      void load();
    } catch (err) {
      toast({
        title: `Failed to ${action} rule`,
        description: err instanceof Error ? err.message : undefined,
        variant: "error",
      });
    }
  };

  return (
    <div className="space-y-4">
      <div>
        <h1 className="flex items-center gap-2 text-lg font-semibold">
          <Globe className="size-5 text-primary" />
          Org-wide rules
        </h1>
        <p className="text-sm text-muted-foreground">
          Approved rules here are injected into every project.
        </p>
      </div>

      <RulesTab
        mandatory={mandatory}
        forbidden={forbidden}
        loading={loading}
        error={error}
        serverMode
        isAdmin
        onAdd={(category) =>
          setEditor({
            open: true,
            ruleType:
              category === "forbidden_rules" ? "forbidden" : "mandatory",
            title: "",
            content: "",
          })
        }
        onEdit={(m) =>
          setEditor({
            open: true,
            ruleType:
              m.category === "forbidden_rules" ? "forbidden" : "mandatory",
            id: m.id,
            title: m.title,
            content: m.content,
          })
        }
        onDelete={(m) => setDeleteTarget(m)}
        onBulkAdd={() =>
          setEditor({ ...CLOSED, open: true, ruleType: "mandatory" })
        }
        onApprove={(m) => void govern(m, "approve")}
        onRevoke={(m) => void govern(m, "revoke")}
      />

      <Dialog open={editor.open} onClose={() => setEditor(CLOSED)}>
        <DialogHeader
          title={editor.id ? "Edit org rule" : "New org rule"}
          onClose={() => setEditor(CLOSED)}
        />
        <DialogBody>
          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="org-rule-title">Title</Label>
              <Input
                id="org-rule-title"
                value={editor.title}
                onChange={(e) =>
                  setEditor((s) => ({ ...s, title: e.target.value }))
                }
                autoFocus
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="org-rule-content">Content</Label>
              <Textarea
                id="org-rule-content"
                rows={4}
                value={editor.content}
                onChange={(e) =>
                  setEditor((s) => ({ ...s, content: e.target.value }))
                }
              />
            </div>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={() => setEditor(CLOSED)}>
            Cancel
          </Button>
          <Button
            onClick={save}
            disabled={saving || !editor.title.trim() || !editor.content.trim()}
          >
            {saving ? "Saving…" : "Save"}
          </Button>
        </DialogFooter>
      </Dialog>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete org rule?"
        description={deleteTarget?.title}
        confirmLabel="Delete"
        destructive
        busy={deleteBusy}
        onConfirm={confirmDelete}
        onClose={() => setDeleteTarget(null)}
      />
    </div>
  );
}
