/**
 * S3 웹사이트: API 베이스 URL(ALB 또는 API Gateway 등). 페이지 오리진과 다를 수 있음.
 * Ingress 확정 후 `k8s/scripts/sync-s3-endpoints-from-ingress.sh`가 S3에 반영.
 * API Gateway로 바꿀 때도 동일 필드에 GW URL만 넣으면 됨(CloudFront로 API를 끼우지 않아도 됨).
 */
window.__TICKETING_API_ORIGIN__ = window.__TICKETING_API_ORIGIN__ || '';
