## 3-repo 구조

| 레포지토리명 | 책임 범위 | 링크 |
|---|---|---|
| soldesk-infra | • 테라폼 코드 전반<br>• AWS 리소스 + IAM<br>• 클러스터 운용에 필요한 애드온 (helm provider) | [github](https://github.com/lhk8080/soldesk-infra) |
| soldesk-k8s | • ArgoCD에 의해 동기화되는 대상<br>• monitoring, service app | [github](https://github.com/lhk8080/soldesk-k8s) |
| **soldesk-app** (이 repo) | • 애플리케이션 소스 코드<br>• 이미지 빌드 & 레지스트리 푸시 지점 | — |

## 디렉토리 구조