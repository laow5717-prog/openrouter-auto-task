#!/bin/bash
# 一键发版：打 tag → 推送触发 Actions → 等待 Release 生成 → 自动下载到 release/
set -e

REPO="laow5717-prog/openrouter-auto-task"
TAG=${1:-}
if [ -z "$TAG" ]; then
    echo "用法: ./release.sh v0.1.7"
    exit 1
fi

echo "===================================="
echo "  发布 $TAG"
echo "===================================="

# 清理同名旧 tag（本地 + 远端），确保重新发版能触发一次全新构建
git tag -d "$TAG" 2>/dev/null || true
git push origin ":refs/tags/$TAG" 2>/dev/null || true

# 打 tag 并推送（触发 GitHub Actions）
git tag "$TAG"
git push origin "$TAG"
echo "✓ Tag $TAG 已推送，等待构建生成 Release..."
echo ""

# 轮询等待 Release 出现并带齐 2 个 zip（build.yml 由各平台直传 Release）
mkdir -p release
COUNT=0
DEADLINE=$((SECONDS + 1800))   # 最多等 30 分钟
while [ $SECONDS -lt $DEADLINE ]; do
    COUNT=$(gh api "repos/$REPO/releases/tags/$TAG" \
        --jq '[.assets[]|select(.name|endswith(".zip"))]|length' 2>/dev/null || echo 0)
    [ -z "$COUNT" ] && COUNT=0
    if [ "$COUNT" -ge 2 ]; then
        echo "✓ Release 已就绪（$COUNT 个产物）"
        break
    fi
    echo "  构建中... 已等待 ${SECONDS}s（当前产物 ${COUNT}/2）"
    sleep 20
done

if [ "$COUNT" -lt 2 ]; then
    echo "✗ 超时：Release 未在 30 分钟内就绪，请检查 GitHub Actions"
    exit 1
fi

# 自动下载到 release/
echo ""
echo "下载产物到 release/ ..."
gh release download "$TAG" -R "$REPO" --dir release/ --clobber

echo ""
echo "===================================="
echo "  完成！产物已下载到 release/"
ls -lh release/*.zip
echo "===================================="
