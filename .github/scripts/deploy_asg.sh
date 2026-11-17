#!/usr/bin/env bash
# Despliega una imagen en el Auto Scaling Group de un ambiente sin tocar Terraform:
#   1. lee qué imagen de ECR descarga la plantilla de lanzamiento del ASG (docker pull ... en user_data);
#   2. publica ahí la imagen nueva (copiada de Docker Hub);
#   3. lanza un instance refresh que crea instancias nuevas ANTES de quitar las viejas;
#   4. smoke test: /healthz responde y la cabecera X-Kut-Version es el commit desplegado.
# Variables: ASG_NAME, SOURCE_IMAGE, PUBLIC_URL, RELEASE.
set -euo pipefail
: "${ASG_NAME:?}" "${SOURCE_IMAGE:?}" "${PUBLIC_URL:?}" "${RELEASE:?}"

read -r LT_ID LT_VERSION < <(aws autoscaling describe-auto-scaling-groups --auto-scaling-group-names "$ASG_NAME" \
  --query 'AutoScalingGroups[0].LaunchTemplate.[LaunchTemplateId,Version]' --output text)
TARGET=$(aws ec2 describe-launch-template-versions --launch-template-id "$LT_ID" --versions "$LT_VERSION" \
  --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text | base64 -d | tr -d '\r' | sed -n 's/^docker pull //p' | head -1)
[[ -n "$TARGET" ]] || { echo "No encontré la imagen en la plantilla $LT_ID v$LT_VERSION"; exit 1; }
echo "ASG $ASG_NAME (plantilla $LT_ID v$LT_VERSION) descarga: $TARGET"

aws ecr get-login-password | docker login --username AWS --password-stdin "${TARGET%%/*}"
docker pull -q "$SOURCE_IMAGE"
docker tag "$SOURCE_IMAGE" "$TARGET"
docker push -q "$TARGET"

REFRESH_ID=$(aws autoscaling start-instance-refresh --auto-scaling-group-name "$ASG_NAME" \
  --preferences '{"MinHealthyPercentage":100,"MaxHealthyPercentage":200,"InstanceWarmup":120,"SkipMatching":false}' \
  --query InstanceRefreshId --output text)
echo "Instance refresh $REFRESH_ID"
for _ in $(seq 1 90); do
  read -r STATUS PCT < <(aws autoscaling describe-instance-refreshes --auto-scaling-group-name "$ASG_NAME" \
    --instance-refresh-ids "$REFRESH_ID" --query 'InstanceRefreshes[0].[Status,PercentageComplete]' --output text)
  echo "$(date +%T) $STATUS ${PCT}%"
  case "$STATUS" in
    Successful) break ;;
    Failed | Cancelled | RollbackSuccessful | RollbackFailed) exit 1 ;;
  esac
  sleep 20
done
[[ "$STATUS" == "Successful" ]] || { echo "El instance refresh no terminó a tiempo"; exit 1; }

for _ in $(seq 1 10); do
  VERSION=$(curl -fsS -o /dev/null -D - "$PUBLIC_URL/healthz" | tr -d '\r' | sed -n 's/^x-kut-version: //Ip')
  echo "$PUBLIC_URL/healthz → X-Kut-Version: ${VERSION:-?}"
  [[ "$VERSION" == "$RELEASE" ]] && { echo "Smoke test OK"; exit 0; }
  sleep 15
done
echo "Smoke test falló: se esperaba la versión $RELEASE"
exit 1
