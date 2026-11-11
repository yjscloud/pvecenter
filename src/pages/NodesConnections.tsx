/* ==========================================================================
   pvecenter — 节点 · 连接配置（子页面）
   ==========================================================================

   这里是 `/nodes` 的第二个子页面：管「面板能连哪些 Proxmox 主机」。

   为什么从「系统设置」搬到这里、并单独成页：
     * 它管的是**节点域**的事 —— 节点连不上时，要修的正是连接；
     * 连接表单很长（主机 / 令牌 / 控制台凭据 / 证书 / 环境自检 / 创建教程），
       混在节点列表下面会把节点列表顶得老远，找节点得先滚过一整张表单；
     * 单独成页后，节点页的「添加节点」按钮可以直接跳过来并展开表单。

   为什么不再有「当前连接 / 默认节点」：见 ConnectionManager 顶部说明。

   卡片 / 列表是同一份数据的两种读法：卡片看一台机器的凭据、节点与自检结果，
   列表把十几台拉成行横向比对。偏好记在 localStorage —— 它只是习惯，不该进
   分享出去的链接。PVE 与 PBS 两张卡共用同一个 mode，避免上半页横排、下半页
   方块这种一半一半的割裂感。
   ========================================================================== */

import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { PageShell } from '../components/Layout';
import { ConnectionManager } from '../components/ConnectionManager';
import { PbsConnectionCard } from '../components/PbsConnectionCard';
import { NodeSubNav } from '../components/NodeSubNav';
import { Card } from '../components/ui/Card';
import { Button } from '../components/ui/Button';
import { EmptyState } from '../components/ui/EmptyState';
import { SegmentedControl } from '../components/ui/Input';
import { IconPlug, IconServer } from '../components/Icons';
import { useAuth } from '../hooks/useAuth';
import { useT } from '../i18n';

type ConnectionViewMode = 'card' | 'list';

const VIEW_KEY = 'pvecenter.nodes.connections-view';

function readView(): ConnectionViewMode {
  try {
    return localStorage.getItem(VIEW_KEY) === 'list' ? 'list' : 'card';
  } catch {
    /* 隐私模式读不到，按默认卡片视图走 */
    return 'card';
  }
}

export function NodesConnections() {
  const t = useT();
  const navigate = useNavigate();
  /* 整页都是管理入口（settings.manage）：非管理员**连只读视图也不给** ——
     这一页列的是主机地址、端口、令牌与控制台凭据的状态。原先只是把表单禁用了，
     信息本身照样摊在页面上，等于给任何账号看了一份集群清单。

     后端也已按角色裁剪字段（routers/config.list_connections），这里只是不让
     人白跑一趟。 */
  const { isAdmin, hasPermission } = useAuth();

  const [viewMode, setViewMode] = useState<ConnectionViewMode>(readView);
  useEffect(() => {
    try {
      localStorage.setItem(VIEW_KEY, viewMode);
    } catch {
      /* 写不进去不影响本次使用 */
    }
  }, [viewMode]);

  return (
    <PageShell
      title={
        <>
          {/* 与左侧导航「集群节点」同一枚机架图标：这一页是节点域的子页面，
              页头图标跟着父入口走，而不是用「插头」讲成另一件事 */}
          <IconServer size={20} />
          {t('nodesConnections.title')}
        </>
      }
      subtitle={t('nodesConnections.subtitle')}
    >
      {/* 视图切换与子页面切换同处一行：左边是「节点 / 连接配置」两个真实路由，
          右边是这张页面的卡片 / 列表偏好。它管的是整页（PVE 与 PBS 两张卡一起
          变），放进某一张卡的标题栏会让另一张卡看起来不受控；放在页头又会与下面
          的卡片右边缘错开一截。 */}
      <div className="subnav-row">
        <NodeSubNav active="connections" />
        {isAdmin ? (
          <SegmentedControl<ConnectionViewMode>
            value={viewMode}
            onChange={setViewMode}
            ariaLabel={t('nodesConnections.viewAria')}
            options={[
              { label: t('common.viewCards'), value: 'card' },
              { label: t('common.viewList'), value: 'list' },
            ]}
          />
        ) : null}
      </div>

      {isAdmin ? (
        <>
          <ConnectionManager isAdmin viewMode={viewMode} />
          {/* PBS 是可选增值接入：凭据体系与 PVE 不同，所以仍是独立一张卡，
              但卡片、列表、编辑弹窗与上面的连接管理共用同一套版式与间距 */}
          {hasPermission('pbs.manage') ? (
            <PbsConnectionCard viewMode={viewMode} />
          ) : null}
        </>
      ) : (
        <Card>
          <EmptyState
            title={t('nodesConnections.noPermissionTitle')}
            description={t('nodesConnections.noPermissionBody')}
            icon={<IconPlug size={30} />}
            action={
              <Button variant="secondary" onClick={() => navigate('/nodes')}>
                {t('nodesConnections.backToNodes')}
              </Button>
            }
          />
        </Card>
      )}
    </PageShell>
  );
}
