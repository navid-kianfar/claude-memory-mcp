import type { Project } from "../types";
import { Dialog, DialogBody, DialogHeader } from "./ui/Dialog";
import { ImportRulesPanel } from "./ImportRulesPanel";

export interface ImportRulesDialogProps {
  open: boolean;
  onClose: () => void;
  /** project the rules will be imported into */
  targetSlug: string;
  targetName: string;
  projects: Project[];
  /** called after a successful import so the parent can refresh */
  onDone: (summary: string) => void;
}

/**
 * Standalone "Import rules" dialog for an existing project. Wraps the same
 * ImportRulesPanel used inside the new-project seeding step.
 */
export function ImportRulesDialog({
  open,
  onClose,
  targetSlug,
  targetName,
  projects,
  onDone,
}: ImportRulesDialogProps) {
  return (
    <Dialog open={open} onClose={onClose} className="max-w-xl">
      <DialogHeader
        title={`Import rules into ${targetName}`}
        description="Seed this project with rules from a template or another project."
        onClose={onClose}
      />
      <DialogBody>
        {open && (
          <ImportRulesPanel
            targetSlug={targetSlug}
            projects={projects}
            onDone={onDone}
          />
        )}
      </DialogBody>
    </Dialog>
  );
}
