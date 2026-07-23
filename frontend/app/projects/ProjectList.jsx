"use client";

import { Archive, Image as ImageIcon, Video } from "lucide-react";
import { formatLocalDateTime } from "../../lib/datetime";

const TYPE_LABELS = { image: "图片", video: "视频", mixed: "混合" };

export default function ProjectList({ projects, activeId, loading, onSelect }) {
  if (loading) {
    return (
      <div className="space-y-2" aria-busy="true" aria-label="项目加载中">
        {Array.from({ length: 4 }).map((_, index) => <div key={index} className="skeleton h-20" />)}
      </div>
    );
  }
  if (!projects.length) {
    return (
      <div className="border-y border-line py-8 text-center" role="status">
        <p className="text-sm text-mist">暂无项目</p>
        <p className="mt-1 text-xs text-fog">在上方创建项目后开始归集素材。</p>
      </div>
    );
  }
  return (
    <ul className="divide-y divide-line" aria-label="项目列表">
      {projects.map((project) => {
        const active = project.id === activeId;
        const TypeIcon = project.project_type === "video" ? Video : ImageIcon;
        return (
          <li key={project.id}>
            <button
              type="button"
              onClick={() => onSelect(project.id)}
              className={`w-full px-3 py-3 text-left transition ${active ? "bg-white/[0.07]" : "hover:bg-white/[0.04]"}`}
              aria-current={active ? "true" : undefined}
            >
              <span className="flex min-w-0 items-center gap-2">
                <TypeIcon size={15} className={active ? "text-aqua" : "text-fog"} aria-hidden="true" />
                <span className="min-w-0 flex-1 truncate text-sm font-display font-medium text-snow">{project.title}</span>
                {project.status === "archived" && <Archive size={13} className="text-fog" aria-label="已归档" />}
              </span>
              <span className="mt-1.5 flex items-center justify-between gap-2 text-[11px] text-fog">
                <span>{TYPE_LABELS[project.project_type] || "混合"} · {project.asset_count} 个素材</span>
                <span>{formatLocalDateTime(project.updated_at)}</span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
