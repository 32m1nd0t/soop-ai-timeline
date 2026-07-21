from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PublicationPlan:
    root_comment: str
    replies: tuple[str, ...]

    @classmethod
    def from_blocks(cls, blocks: list[str]) -> "PublicationPlan":
        if not blocks:
            raise ValueError("게시할 타임라인 블록이 없습니다.")
        return cls(root_comment=blocks[0], replies=tuple(blocks[1:]))


class PublisherNotConfiguredError(RuntimeError):
    pass


class SoopApiPublisher:
    """Official API integration seam.

    Once SOOP grants comment/reply scopes, this class will use OAuth tokens and
    persist each returned comment id so retries cannot create duplicate posts.
    """

    available = False

    def publish(self, vod_id: str, plan: PublicationPlan) -> None:
        del vod_id, plan
        raise PublisherNotConfiguredError(
            "SOOP 공식 댓글 API 권한이 연결되지 않았습니다."
        )

