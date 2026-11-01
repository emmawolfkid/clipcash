"""
Video integrity rules (PostgreSQL): videos and categories are never deleted
(archive videos, deactivate categories), and ARCHIVED is terminal.
The table owner can disable these triggers; see docs/architecture/videos.md.
"""

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION video_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'videos cannot be deleted; archive them' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.publication_status = 'ARCHIVED' AND NEW.publication_status <> 'ARCHIVED' THEN
        RAISE EXCEPTION 'archived videos cannot be republished' USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.provider_video_id <> '' AND NEW.provider_video_id <> OLD.provider_video_id THEN
        RAISE EXCEPTION 'a video''s provider id cannot change' USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER video_guard BEFORE UPDATE OR DELETE ON videos_video
    FOR EACH ROW EXECUTE FUNCTION video_guard();

CREATE OR REPLACE FUNCTION video_category_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'video categories cannot be deleted; deactivate them' USING ERRCODE = 'insufficient_privilege';
END;
$$;

CREATE TRIGGER video_category_guard BEFORE DELETE ON videos_videocategory
    FOR EACH ROW EXECUTE FUNCTION video_category_guard();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS video_category_guard ON videos_videocategory;
DROP FUNCTION IF EXISTS video_category_guard();
DROP TRIGGER IF EXISTS video_guard ON videos_video;
DROP FUNCTION IF EXISTS video_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("videos", "0001_initial")]

    operations = [migrations.RunSQL(FORWARD, REVERSE)]
