DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema='public' AND table_type='BASE TABLE'
  LOOP
    -- guild_id + level
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='guild_id'
    ) AND EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='level'
    ) THEN
      EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I (%I, %I)',
        'uq_' || r.table_name || '_guild_level', r.table_name, 'guild_id', 'level');
    END IF;

    -- reaction block candidates
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='message_id'
    ) AND EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='blocked_role_id'
    ) THEN
      EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I (%I, %I)',
        'uq_' || r.table_name || '_message_blocked_role', r.table_name, 'message_id', 'blocked_role_id');
    END IF;

    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='guild_id'
    ) AND EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='message_id'
    ) AND EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='blocked_role_id'
    ) THEN
      EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I (%I, %I, %I)',
        'uq_' || r.table_name || '_guild_message_blocked_role', r.table_name, 'guild_id', 'message_id', 'blocked_role_id');
    END IF;

    -- reaction role candidates
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='message_id'
    ) AND EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='emoji'
    ) THEN
      EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I (%I, %I)',
        'uq_' || r.table_name || '_message_emoji', r.table_name, 'message_id', 'emoji');
    END IF;

    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='guild_id'
    ) AND EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='message_id'
    ) AND EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema='public' AND table_name=r.table_name AND column_name='emoji'
    ) THEN
      EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS %I ON %I (%I, %I, %I)',
        'uq_' || r.table_name || '_guild_message_emoji', r.table_name, 'guild_id', 'message_id', 'emoji');
    END IF;
  END LOOP;
END $$;
