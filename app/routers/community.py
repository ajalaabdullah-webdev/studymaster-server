"""Community, profile, follow and gaming endpoints — ported from Node.

    GET/POST /api/v1/community/posts            (POST is multipart: content + image?)
    PUT      /api/v1/community/posts/{id}/like
    GET/POST /api/v1/community/posts/{id}/comments
    GET      /api/v1/profile/{userId}
    POST/DELETE /api/v1/follow/{userId}
    POST     /api/v1/game/attempt
    GET      /api/v1/game/leaderboard
"""

from typing import Any, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.core.deps import get_current_user
from app.models.community import Comment, Follower, GameAttempt, Post
from app.models.user import User
from app.services import media

community = APIRouter(prefix="/api/v1/community", tags=["community"])
profile = APIRouter(prefix="/api/v1/profile", tags=["community"])
follow = APIRouter(prefix="/api/v1/follow", tags=["community"])
game = APIRouter(prefix="/api/v1/game", tags=["gaming"])


async def _author(user_id: PydanticObjectId) -> dict[str, Any]:
    u = await User.get(user_id)
    return {"_id": str(user_id), "username": u.username if u else "Unknown",
            "avatar": u.avatar if u else "", "bio": u.bio if u else ""}


async def _post_dict(p: Post) -> dict[str, Any]:
    d = p.model_dump(mode="json")
    d["_id"] = str(p.id)
    d["userId"] = await _author(p.userId)
    d["likes"] = [str(x) for x in (p.likes or [])]
    d["commentCount"] = await Comment.find(Comment.postId == p.id).count()
    return d


# ------------------------------- community -------------------------------- #
@community.get("/posts")
async def get_posts() -> dict:
    posts = await Post.find_all(sort=[("createdAt", -1)]).to_list()
    data = [await _post_dict(p) for p in posts]
    return {"success": True, "count": len(data), "data": data}


@community.post("/posts")
async def create_post(
    content: str = Form(...),
    image: Optional[UploadFile] = File(default=None),
    current: User = Depends(get_current_user),
) -> dict:
    url = ""
    if image is not None:
        raw = await image.read()
        url = media.upload_image(raw) or ""
    post = Post(userId=current.id, content=content, image=url)
    await post.insert()
    return {"success": True, "data": await _post_dict(post)}


@community.put("/posts/{post_id}/like")
async def like_post(post_id: str, current: User = Depends(get_current_user)) -> dict:
    post = await _get_post(post_id)
    if current.id in (post.likes or []):
        post.likes = [x for x in post.likes if x != current.id]
    else:
        post.likes = (post.likes or []) + [current.id]
    await post.save()
    return {"success": True, "data": await _post_dict(post)}


@community.get("/posts/{post_id}/comments")
async def get_comments(post_id: str) -> dict:
    post = await _get_post(post_id)
    comments = await Comment.find(Comment.postId == post.id).sort(+Comment.createdAt).to_list()
    data = []
    for c in comments:
        d = c.model_dump(mode="json")
        d["_id"] = str(c.id)
        d["userId"] = await _author(c.userId)
        data.append(d)
    return {"success": True, "data": data}


class CommentRequest(BaseModel):
    content: str


@community.post("/posts/{post_id}/comments")
async def add_comment(post_id: str, body: CommentRequest, current: User = Depends(get_current_user)) -> dict:
    post = await _get_post(post_id)
    comment = Comment(userId=current.id, postId=post.id, content=body.content)
    await comment.insert()
    d = comment.model_dump(mode="json")
    d["_id"] = str(comment.id)
    d["userId"] = await _author(current.id)
    return {"success": True, "data": d}


async def _get_post(post_id: str) -> Post:
    if not PydanticObjectId.is_valid(post_id):
        raise HTTPException(status_code=400, detail="Invalid post id")
    post = await Post.get(PydanticObjectId(post_id))
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


# -------------------------------- profile --------------------------------- #
@profile.get("/{user_id}")
async def get_profile(user_id: str, current: User = Depends(get_current_user)) -> dict:
    if not PydanticObjectId.is_valid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user id")
    uid = PydanticObjectId(user_id)
    user = await User.get(uid)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    followers = await Follower.find(Follower.followingId == uid).count()
    following = await Follower.find(Follower.followerId == uid).count()
    is_following = await Follower.find_one(
        Follower.followerId == current.id, Follower.followingId == uid
    ) is not None

    user_posts = await Post.find(Post.userId == uid).sort(-Post.createdAt).to_list()
    posts = [await _post_dict(p) for p in user_posts]
    total_likes = sum(len(p.likes or []) for p in user_posts)

    liked = await Post.find({"likes": uid}).sort(-Post.createdAt).to_list()
    liked_posts = [await _post_dict(p) for p in liked]

    commented_ids = {c.postId for c in await Comment.find(Comment.userId == uid).to_list()}
    commented = await Post.find({"_id": {"$in": list(commented_ids)}}).sort(-Post.createdAt).to_list()
    commented_posts = [await _post_dict(p) for p in commented]

    return {
        "success": True,
        "data": {
            "user": {"id": str(uid), "username": user.username, "avatar": user.avatar,
                     "bio": user.bio, "class": user.onboarding.class_},
            "stats": {"followers": followers, "following": following, "totalLikes": total_likes,
                      "totalPosts": len(posts), "isFollowing": is_following},
            "posts": posts, "likedPosts": liked_posts, "commentedPosts": commented_posts,
        },
    }


# --------------------------------- follow ---------------------------------- #
@follow.post("/{user_id}")
async def follow_user(user_id: str, current: User = Depends(get_current_user)) -> dict:
    if not PydanticObjectId.is_valid(user_id) or PydanticObjectId(user_id) == current.id:
        raise HTTPException(status_code=400, detail="Invalid user")
    uid = PydanticObjectId(user_id)
    existing = await Follower.find_one(Follower.followerId == current.id, Follower.followingId == uid)
    if not existing:
        await Follower(followerId=current.id, followingId=uid).insert()
    return {"success": True, "message": "Followed"}


@follow.delete("/{user_id}")
async def unfollow_user(user_id: str, current: User = Depends(get_current_user)) -> dict:
    if PydanticObjectId.is_valid(user_id):
        await Follower.find(
            Follower.followerId == current.id, Follower.followingId == PydanticObjectId(user_id)
        ).delete()
    return {"success": True, "message": "Unfollowed"}


# ---------------------------------- game ----------------------------------- #
@game.get("/available")
async def available_games(current: User = Depends(get_current_user)) -> dict:
    """Which games students can play right now (admin-toggled)."""
    from app.models.platform import get_platform_settings

    ps = await get_platform_settings()
    enabled = [g.model_dump() for g in ps.games if g.enabled] if ps.gamingEnabled else []
    return {"success": True, "data": {"gamingEnabled": ps.gamingEnabled, "games": enabled}}


class AttemptRequest(BaseModel):
    timeTaken: int
    correctCount: int


@game.post("/attempt")
async def save_attempt(body: AttemptRequest, current: User = Depends(get_current_user)) -> dict:
    attempt = GameAttempt(userId=current.id, timeTaken=body.timeTaken, correctCount=body.correctCount)
    await attempt.insert()
    return {"success": True, "data": {**attempt.model_dump(mode="json"), "_id": str(attempt.id)}}


@game.get("/leaderboard")
async def leaderboard(current: User = Depends(get_current_user)) -> dict:
    rows = await GameAttempt.aggregate([
        {"$match": {"correctCount": 10}},
        {"$group": {"_id": "$userId", "bestTime": {"$min": "$timeTaken"},
                    "totalAttempts": {"$sum": 1}, "lastAttemptAt": {"$max": "$createdAt"}}},
        {"$sort": {"bestTime": 1}},
        {"$limit": 10},
    ]).to_list()
    data = []
    for r in rows:
        u = await User.get(r["_id"])
        data.append({
            "userId": str(r["_id"]),
            "username": u.username if u else "Unknown",
            "avatar": u.avatar if u else "",
            "bestTime": r["bestTime"],
            "totalAttempts": r["totalAttempts"],
        })
    return {"success": True, "data": data}
