export interface CrudRoutingFormFields {
  replica_host?: unknown;
  replica_port?: unknown;
  enable_crud?: unknown;
}

export interface NormalizedCrudRoutingFormFields {
  replica_host: string | null;
  replica_port: number | null;
  enable_crud: boolean;
}

export function normalizeCrudRoutingFormFields(
  fields: CrudRoutingFormFields
): NormalizedCrudRoutingFormFields {
  const replicaHost = typeof fields.replica_host === "string" ? fields.replica_host.trim() : "";
  return {
    replica_host: replicaHost || null,
    replica_port: replicaHost && typeof fields.replica_port === "number" ? fields.replica_port : null,
    enable_crud: fields.enable_crud === true
  };
}
